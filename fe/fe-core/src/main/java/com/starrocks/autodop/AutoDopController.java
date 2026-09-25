// Copyright 2021-present StarRocks, Inc. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package com.starrocks.autodop;

import com.starrocks.common.Config;
import com.starrocks.common.StarRocksException;
import com.starrocks.qe.ConnectContext;
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.JobSpec;
import com.starrocks.thrift.TQueryOptions;
import com.starrocks.thrift.TQueryType;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.IOException;
import java.util.Map;

public final class AutoDopController {
    private static final Logger LOG = LogManager.getLogger(AutoDopController.class);
    private static volatile AutoDopClient clientForTest;

    private AutoDopController() {
    }

    public static boolean isEnabled(ConnectContext context) {
        return Config.enable_auto_dop || context != null && context.getSessionVariable() != null
                && context.getSessionVariable().isEnableAutoDop();
    }

    /**
     * Decides whether the query may enter the AutoDOP decision path (DD01 §3.1-P4).
     *
     * <p>Unlike the previous {@code checkSupported}, this never throws: unsupported queries
     * (non-SELECT, pipeline disabled, runtime adaptive DOP enabled, missing context,
     * fewer than five plan fragments) simply
     * bypass AutoDOP and keep the original DOP, so {@code enable_auto_dop=true} no longer
     * fails DML or other non-SELECT statements.
     */
    public static boolean isSupported(ConnectContext context, JobSpec jobSpec) {
        if (context == null || context.getSessionVariable() == null || jobSpec == null) {
            LOG.warn("AutoDOP skipped: missing query context");
            return false;
        }
        return isSupported(context, jobSpec.getQueryOptions(), jobSpec.isEnablePipeline(),
                jobSpec.getFragments().size());
    }

    /**
     * Package-visible seam of {@link #isSupported(ConnectContext, JobSpec)} that only needs the
     * extracted inputs; unit tests exercise the gate without constructing a {@link JobSpec}.
     */
    static boolean isSupported(ConnectContext context, TQueryOptions queryOptions, boolean pipelineEnabled,
                               int fragmentCount) {
        // Count plan fragments, including the gather root, not fragment instances.
        // This gate runs before placement, feature extraction and the decision RPC.
        if (fragmentCount < 5) {
            return false;
        }
        if (queryOptions == null || queryOptions.getQuery_type() != TQueryType.SELECT) {
            LOG.warn("AutoDOP skipped: only SELECT queries are supported");
            return false;
        }
        if (!pipelineEnabled) {
            LOG.warn("AutoDOP skipped: requires pipeline execution");
            return false;
        }
        if (context.getSessionVariable().isEnableRuntimeAdaptiveDop()) {
            LOG.warn("AutoDOP skipped: requires enable_runtime_adaptive_dop=false");
            return false;
        }
        return true;
    }

    public static AutoDopResult decideAndInstall(
            ConnectContext context, JobSpec jobSpec, ExecutionDAG executionDAG) throws StarRocksException {
        Map<String, Integer> originalDop = AutoDopDecisionApplier.captureDop(jobSpec.getFragments());
        AutoDopRequest request = AutoDopFeatureExtractor.extract(context, jobSpec, executionDAG, originalDop);
        AutoDopResult result = decide(request);
        if (result.getSkipReason() != null) {
            // degraded decision: nothing installed, the query keeps the original DOP
            return result;
        }
        AutoDopDecisionApplier.apply(jobSpec.getFragments(), result.getDecidedDop());
        Map<String, Integer> installedDop = AutoDopDecisionApplier.captureDop(jobSpec.getFragments());
        return new AutoDopResult(request, result.getResponse(), originalDop, result.getDecidedDop(),
                installedDop, result.getDecisionLatencyMs());
    }

    /**
     * Calls the decision service and validates the response. Package-visible seam so unit tests
     * exercise the failure/degradation paths with a mock client and a plain request, without
     * building a full {@link JobSpec}/{@link ExecutionDAG}. Returns a result whose
     * {@link AutoDopResult#getSkipReason()} is non-null when AutoDOP was skipped.
     */
    static AutoDopResult decide(AutoDopRequest request) throws StarRocksException {
        long startMs = System.currentTimeMillis();
        AutoDopResponse response;
        try {
            response = client().decide(request);
        } catch (IOException e) {
            if (!Config.auto_dop_fail_on_error) {
                LOG.warn("AutoDOP skipped: decision request failed: {}", e.getMessage());
                return new AutoDopResult(request, null, null, null, null, 0L,
                        "decision request failed: " + e.getMessage());
            }
            throw new StarRocksException("AutoDOP decision request failed", e);
        }
        long decisionLatencyMs = System.currentTimeMillis() - startMs;
        Map<String, Integer> decisions;
        try {
            decisions = AutoDopDecisionValidator.validateOrThrow(request, response);
        } catch (StarRocksException e) {
            if (!Config.auto_dop_fail_on_error) {
                LOG.warn("AutoDOP skipped: invalid decision response: {}", e.getMessage());
                return new AutoDopResult(request, null, null, null, null, decisionLatencyMs,
                        "invalid decision response: " + e.getMessage());
            }
            throw e;
        }
        return new AutoDopResult(request, response, null, decisions, null, decisionLatencyMs);
    }

    private static AutoDopClient client() {
        return clientForTest == null
                ? new HttpAutoDopClient(Config.auto_dop_endpoint, Config.auto_dop_timeout_ms)
                : clientForTest;
    }

    public static void setClientForTest(AutoDopClient client) {
        clientForTest = client;
    }

    public static void resetClientForTest() {
        clientForTest = null;
    }
}
