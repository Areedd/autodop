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
import com.starrocks.qe.SessionVariable;
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.JobSpec;
import com.starrocks.thrift.TQueryType;

import java.io.IOException;
import java.util.Map;

public final class AutoDopController {
    private static volatile AutoDopClient clientForTest;

    private AutoDopController() {
    }

    public static boolean isEnabled(ConnectContext context) {
        return Config.enable_auto_dop || context != null && context.getSessionVariable() != null
                && context.getSessionVariable().isEnableAutoDop();
    }

    public static void checkSupported(ConnectContext context, JobSpec jobSpec) throws StarRocksException {
        if (context == null || context.getSessionVariable() == null || jobSpec == null) {
            throw new StarRocksException("AutoDOP requires a query context");
        }
        if (jobSpec.getQueryOptions() == null || jobSpec.getQueryOptions().getQuery_type() != TQueryType.SELECT) {
            throw new StarRocksException("AutoDOP only supports SELECT queries");
        }
        if (!jobSpec.isEnablePipeline()) {
            throw new StarRocksException("AutoDOP requires pipeline execution");
        }
        SessionVariable session = context.getSessionVariable();
        if (session.isEnableRuntimeAdaptiveDop()) {
            throw new StarRocksException("AutoDOP requires enable_runtime_adaptive_dop=false");
        }
    }

    public static AutoDopResult decideAndInstall(
            ConnectContext context, JobSpec jobSpec, ExecutionDAG executionDAG) throws StarRocksException {
        Map<String, Integer> originalDop = AutoDopDecisionApplier.captureDop(jobSpec.getFragments());
        AutoDopRequest request = AutoDopFeatureExtractor.extract(context, jobSpec, executionDAG, originalDop);
        long startMs = System.currentTimeMillis();
        AutoDopResponse response;
        try {
            response = client().decide(request);
        } catch (IOException e) {
            throw new StarRocksException("AutoDOP decision request failed", e);
        }
        long decisionLatencyMs = System.currentTimeMillis() - startMs;
        Map<String, Integer> decisions = AutoDopDecisionValidator.validateOrThrow(request, response);
        AutoDopDecisionApplier.apply(jobSpec.getFragments(), decisions);
        Map<String, Integer> installedDop = AutoDopDecisionApplier.captureDop(jobSpec.getFragments());
        return new AutoDopResult(
                request, response, originalDop, decisions, installedDop, decisionLatencyMs);
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
