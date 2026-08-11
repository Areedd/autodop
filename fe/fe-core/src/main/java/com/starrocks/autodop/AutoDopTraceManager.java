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

import com.google.common.collect.Lists;
import com.google.gson.annotations.SerializedName;
import com.starrocks.common.Config;
import com.starrocks.common.ThreadPoolManager;
import com.starrocks.common.util.DebugUtil;
import com.starrocks.persist.gson.GsonUtils;
import com.starrocks.planner.PlanFragment;
import com.starrocks.proto.PQueryStatistics;
import com.starrocks.qe.ConnectContext;
import com.starrocks.qe.QueryDetail;
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.ExecutionFragment;
import com.starrocks.qe.scheduler.dag.FragmentInstance;
import com.starrocks.thrift.TScanRangeParams;
import com.starrocks.thrift.TUniqueId;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

public final class AutoDopTraceManager {
    private static final Logger LOG = LogManager.getLogger(AutoDopTraceManager.class);
    private static final ConcurrentMap<String, TraceRecord> TRACES = new ConcurrentHashMap<>();
    private static final ExecutorService WRITER = ThreadPoolManager.newDaemonFixedThreadPool(
            1, Integer.MAX_VALUE, "auto-dop-trace-writer", true);

    private static volatile String traceDirForTest;

    private AutoDopTraceManager() {
    }

    public static void register(AutoDopResult result) {
        if (result == null || result.getRequest() == null) {
            return;
        }
        TraceRecord record = new TraceRecord();
        record.queryId = result.getRequest().queryId;
        record.autoDop.decisionLatencyMs = result.getDecisionLatencyMs();
        record.autoDop.originalDop = result.getOriginalDop();
        record.autoDop.decidedDop = result.getDecidedDop();
        record.autoDop.installedDop = result.getInstalledDop();
        record.request = result.getRequest();
        record.response = result.getResponse();
        TRACES.put(record.queryId, record);
    }

    public static void captureActualDop(TUniqueId queryId, ExecutionDAG executionDAG) {
        TraceRecord record = TRACES.get(DebugUtil.printId(queryId));
        if (record == null || executionDAG == null) {
            return;
        }

        Map<String, List<Integer>> actualInstanceDop = new LinkedHashMap<>();
        Map<String, ActualFragmentDop> fragmentsById = new java.util.HashMap<>();
        for (ActualFragmentDop actual : record.actualFragments) {
            fragmentsById.put(actual.fragmentId, actual);
        }
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInCreatedOrder()) {
            PlanFragment fragment = execFragment.getPlanFragment();
            ActualFragmentDop actual = fragmentsById.get(fragment.getFragmentId().toString());
            if (actual == null) {
                actual = new ActualFragmentDop();
                actual.fragmentId = fragment.getFragmentId().toString();
                actual.planDop = fragment.getPipelineDop();
                for (FragmentInstance instance : execFragment.getInstances()) {
                    actual.workerIds.add(instance.getWorkerId());
                    actual.scanRangeCounts.add(scanRangeCount(instance));
                    actual.driverSeqScanRangeCounts.add(driverSeqScanRangeCount(instance));
                }
                record.actualFragments.add(actual);
            }
            for (FragmentInstance instance : execFragment.getInstances()) {
                actual.instanceDops.add(instance.getPipelineDop());
            }
            actualInstanceDop.put(actual.fragmentId, actual.instanceDops);
        }
        record.autoDop.actualInstanceDop = actualInstanceDop;
    }

    /**
     * Captures a per-instance DOP snapshot at one of the two intermediate stages of the
     * decision window: {@link InstanceDopStage#BASELINE} right after instances are materialized
     * (before the instance-level adjustment) and {@link InstanceDopStage#ADJUSTED} right after
     * {@link AutoDopInstanceAdjuster} runs. The final snapshot is captured by
     * {@link #captureActualDop} after {@code finalizeDAG}.
     */
    public static void captureInstanceDop(TUniqueId queryId, ExecutionDAG executionDAG,
                                          InstanceDopStage stage) {
        TraceRecord record = TRACES.get(DebugUtil.printId(queryId));
        if (record == null || executionDAG == null) {
            return;
        }
        if (record.actualFragments.isEmpty()) {
            for (ExecutionFragment execFragment : executionDAG.getFragmentsInCreatedOrder()) {
                ActualFragmentDop actual = new ActualFragmentDop();
                actual.fragmentId = execFragment.getFragmentId().toString();
                actual.planDop = execFragment.getPlanFragment().getPipelineDop();
                for (FragmentInstance instance : execFragment.getInstances()) {
                    actual.workerIds.add(instance.getWorkerId());
                    actual.scanRangeCounts.add(scanRangeCount(instance));
                    actual.driverSeqScanRangeCounts.add(driverSeqScanRangeCount(instance));
                }
                record.actualFragments.add(actual);
            }
        }
        Map<String, ActualFragmentDop> fragmentsById = new java.util.HashMap<>();
        for (ActualFragmentDop actual : record.actualFragments) {
            fragmentsById.put(actual.fragmentId, actual);
        }
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInCreatedOrder()) {
            ActualFragmentDop actual = fragmentsById.get(execFragment.getFragmentId().toString());
            if (actual == null) {
                continue;
            }
            List<Integer> snapshot = new java.util.ArrayList<>();
            for (FragmentInstance instance : execFragment.getInstances()) {
                snapshot.add(instance.getPipelineDop());
            }
            if (stage == InstanceDopStage.BASELINE) {
                actual.baselineInstanceDops = snapshot;
            } else {
                actual.adjustedInstanceDops = snapshot;
            }
        }
    }

    public enum InstanceDopStage {
        BASELINE,
        ADJUSTED
    }

    public static void finishAndPersist(ConnectContext context, QueryDetail queryDetail, PQueryStatistics statistics) {
        if (context == null || context.getExecutionId() == null) {
            return;
        }
        String queryId = DebugUtil.printId(context.getExecutionId());
        TraceRecord record = TRACES.remove(queryId);
        if (record == null) {
            return;
        }

        if (queryDetail != null) {
            record.state = String.valueOf(queryDetail.getState());
            record.latencyMs = queryDetail.getLatency();
            record.errorMessage = queryDetail.getErrorMessage();
        } else {
            record.state = context.getState() == null ? "" : context.getState().toString();
            record.latencyMs = System.currentTimeMillis() - context.getStartTime();
        }
        record.queryStatistics = QueryStatistics.from(statistics);

        WRITER.submit(() -> writeTrace(record));
    }

    public static void setTraceDirForTest(String traceDir) {
        traceDirForTest = traceDir;
    }

    /**
     * Test hook: exposes the three-stage instance DOP snapshots of the active trace record.
     */
    public static List<ActualFragmentDop> traceFragmentsForTest(TUniqueId queryId) {
        TraceRecord record = TRACES.get(DebugUtil.printId(queryId));
        return record == null ? null : record.actualFragments;
    }

    public static void clearForTest() {
        TRACES.clear();
        traceDirForTest = null;
    }

    public static void flushForTest() throws Exception {
        Future<?> future = WRITER.submit(() -> {
        });
        future.get(5, TimeUnit.SECONDS);
    }

    private static void writeTrace(TraceRecord record) {
        try {
            Path dir = Paths.get(traceDirForTest == null ? Config.auto_dop_trace_dir : traceDirForTest);
            Files.createDirectories(dir);
            Path file = dir.resolve("autodop_trace_" + LocalDate.now() + ".jsonl");
            String line = GsonUtils.GSON.toJson(record) + System.lineSeparator();
            Files.write(file, line.getBytes(StandardCharsets.UTF_8),
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException e) {
            LOG.warn("failed to write AutoDOP trace for query {}", record.queryId, e);
        }
    }

    private static int scanRangeCount(FragmentInstance instance) {
        int count = 0;
        for (List<TScanRangeParams> scanRanges : instance.getNode2ScanRanges().values()) {
            count += scanRanges.size();
        }
        for (Map<Integer, List<TScanRangeParams>> driverSeqToScanRanges :
                instance.getNode2DriverSeqToScanRanges().values()) {
            for (List<TScanRangeParams> scanRanges : driverSeqToScanRanges.values()) {
                count += scanRanges.size();
            }
        }
        return count;
    }

    private static Map<Integer, Integer> driverSeqScanRangeCount(FragmentInstance instance) {
        Map<Integer, Integer> counts = new LinkedHashMap<>();
        for (Map<Integer, List<TScanRangeParams>> driverSeqToScanRanges :
                instance.getNode2DriverSeqToScanRanges().values()) {
            for (Map.Entry<Integer, List<TScanRangeParams>> entry : driverSeqToScanRanges.entrySet()) {
                counts.merge(entry.getKey(), entry.getValue().size(), Integer::sum);
            }
        }
        return counts;
    }

    public static class TraceRecord {
        @SerializedName("version")
        public int version = 1;

        @SerializedName("query_id")
        public String queryId = "";

        @SerializedName("state")
        public String state = "";

        @SerializedName("latency_ms")
        public long latencyMs = -1;

        @SerializedName("error_message")
        public String errorMessage = "";

        @SerializedName("auto_dop")
        public AutoDopSection autoDop = new AutoDopSection();

        @SerializedName("request")
        public AutoDopRequest request;

        @SerializedName("response")
        public AutoDopResponse response;

        @SerializedName("actual_fragments")
        public List<ActualFragmentDop> actualFragments = Lists.newArrayList();

        @SerializedName("query_statistics")
        public QueryStatistics queryStatistics = new QueryStatistics();
    }

    public static class AutoDopSection {
        @SerializedName("decision_latency_ms")
        public long decisionLatencyMs;

        @SerializedName("original_dop")
        public Map<String, Integer> originalDop = new LinkedHashMap<>();

        @SerializedName("decided_dop")
        public Map<String, Integer> decidedDop = new LinkedHashMap<>();

        @SerializedName("installed_dop")
        public Map<String, Integer> installedDop = new LinkedHashMap<>();

        @SerializedName("actual_instance_dop")
        public Map<String, List<Integer>> actualInstanceDop = new LinkedHashMap<>();
    }

    public static class ActualFragmentDop {
        @SerializedName("fragment_id")
        public String fragmentId = "";

        @SerializedName("plan_dop")
        public int planDop;

        // Instance DOP right after materialization, before the instance-level adjustment.
        @SerializedName("baseline_instance_dops")
        public List<Integer> baselineInstanceDops;

        // Instance DOP right after the instance-level adjustment, before finalizeDAG.
        @SerializedName("adjusted_instance_dops")
        public List<Integer> adjustedInstanceDops;

        // Instance DOP after finalizeDAG: the actual DOP sent to the BE.
        @SerializedName("instance_dops")
        public List<Integer> instanceDops = Lists.newArrayList();

        @SerializedName("worker_ids")
        public List<Long> workerIds = Lists.newArrayList();

        @SerializedName("scan_range_counts")
        public List<Integer> scanRangeCounts = Lists.newArrayList();

        @SerializedName("driver_seq_scan_range_counts")
        public List<Map<Integer, Integer>> driverSeqScanRangeCounts = Lists.newArrayList();
    }

    public static class QueryStatistics {
        @SerializedName("scan_rows")
        public long scanRows = -1;

        @SerializedName("scan_bytes")
        public long scanBytes = -1;

        @SerializedName("cpu_cost_ns")
        public long cpuCostNs = -1;

        @SerializedName("mem_cost_bytes")
        public long memCostBytes = -1;

        @SerializedName("spill_bytes")
        public long spillBytes = -1;

        static QueryStatistics from(PQueryStatistics statistics) {
            QueryStatistics result = new QueryStatistics();
            if (statistics == null) {
                return result;
            }
            result.scanRows = statistics.scanRows == null ? -1 : statistics.scanRows;
            result.scanBytes = statistics.scanBytes == null ? -1 : statistics.scanBytes;
            result.cpuCostNs = statistics.cpuCostNs == null ? -1 : statistics.cpuCostNs;
            result.memCostBytes = statistics.memCostBytes == null ? -1 : statistics.memCostBytes;
            result.spillBytes = statistics.spillBytes == null ? -1 : statistics.spillBytes;
            return result;
        }
    }
}
