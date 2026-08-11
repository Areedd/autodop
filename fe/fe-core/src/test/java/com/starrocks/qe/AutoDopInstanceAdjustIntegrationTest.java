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

package com.starrocks.qe;

import com.google.api.client.util.Lists;
import com.starrocks.autodop.AutoDopController;
import com.starrocks.autodop.AutoDopRequest;
import com.starrocks.autodop.AutoDopResourceView;
import com.starrocks.autodop.AutoDopResponse;
import com.starrocks.autodop.AutoDopTraceManager;
import com.starrocks.common.Config;
import com.starrocks.common.ConfigBase;
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.ExecutionFragment;
import com.starrocks.qe.scheduler.dag.FragmentInstance;
import com.starrocks.sql.plan.DistributedEnvPlanTestBase;
import com.starrocks.sql.plan.ExecPlan;
import com.starrocks.thrift.TUniqueId;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Integration tests for the instance-level DOP adjustment (DD04 §6.3): the adjustment runs inside
 * the decision window of {@link CoordinatorPreprocessor}, downscales scan instances on loaded BEs
 * only, keeps routing-safe fragments unchanged, and records the three-stage trace snapshots.
 */
public class AutoDopInstanceAdjustIntegrationTest extends DistributedEnvPlanTestBase {
    private static final long LOADED_WORKER = 10001L;
    private static final long IDLE_WORKER = 10002L;

    private AutoDopResourceView resourceView;

    @BeforeEach
    public void setUp() {
        resourceView = AutoDopResourceView.newInstanceForTest();
        AutoDopResourceView.setInstanceForTest(resourceView);
        Config.auto_dop_instance_adjust_enabled = true;
        Config.auto_dop_instance_adjust_floor = 0.5;
        Config.auto_dop_load_cpu_threshold = 0.4;
        Config.auto_dop_load_mem_threshold = 0.65;
        Config.auto_dop_load_ewma_alpha = 0.25;
    }

    @AfterEach
    public void tearDown() {
        resourceView.shutdown();
        AutoDopResourceView.resetInstanceForTest();
        AutoDopController.resetClientForTest();
        AutoDopTraceManager.clearForTest();
    }

    @Test
    public void testScanInstancesDownscaledOnLoadedWorkerOnly() throws Exception {
        // The mock decision service returns DOP 4 for every fragment. All three workers are
        // injected as loaded so that every eligible scan instance must be downscaled, regardless
        // of the (nondeterministic) placement of the mock cluster.
        AtomicReference<AutoDopRequest> capturedRequest = new AtomicReference<>();
        AutoDopController.setClientForTest(request -> {
            capturedRequest.set(request);
            // Inject the resource view inside the decision call, before materialization.
            resourceView.putLoadForTest(LOADED_WORKER, 0.9, 0.9, true);
            resourceView.putLoadForTest(IDLE_WORKER, 0.9, 0.9, true);
            resourceView.putLoadForTest(10003L, 0.9, 0.9, true);
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = request.queryId;
            for (AutoDopRequest.FragmentInfo fragment : request.fragments) {
                response.decisions.add(new AutoDopResponse.Decision(fragment.fragmentId, 4, "test"));
            }
            return response;
        });
        connectContext.getSessionVariable().setEnableAutoDop(true);
        connectContext.getSessionVariable().setEnablePipelineEngine(true);
        connectContext.getSessionVariable().setEnableRuntimeAdaptiveDop(false);
        connectContext.setExecutionId(new TUniqueId(0x51, 0x0));

        try {
            ExecPlan execPlan = getExecPlan("select count(*) from lineorder_new_l group by P_SIZE");
            CoordinatorPreprocessor prepare = new CoordinatorPreprocessor(
                    execPlan.getFragments(), execPlan.getScanNodes(), connectContext);
            prepare.computeFragmentInstances();

            // Same eligibility rule as AutoDopInstanceAdjuster: no upstream exchange input,
            // not colocate, not bucket shuffle.
            Set<String> destFragmentIds = new HashSet<>();
            for (AutoDopRequest.EdgeInfo edge : capturedRequest.get().edges) {
                destFragmentIds.add(edge.destFragmentId);
            }

            ExecutionDAG dag = prepare.getExecutionDAG();
            boolean scanFragmentSeen = false;
            boolean adjustedSeen = false;
            for (ExecutionFragment execFragment : dag.getFragmentsInCreatedOrder()) {
                String fragmentId = execFragment.getFragmentId().toString();
                boolean eligible = !destFragmentIds.contains(fragmentId)
                        && !execFragment.isColocated()
                        && !execFragment.isLocalBucketShuffleJoin();
                Map<Long, Integer> dopByWorker = new HashMap<>();
                for (FragmentInstance instance : execFragment.getInstances()) {
                    dopByWorker.merge(instance.getWorkerId(), instance.getPipelineDop(), Math::max);
                }
                if (eligible) {
                    scanFragmentSeen = true;
                    for (FragmentInstance instance : execFragment.getInstances()) {
                        Assertions.assertTrue(instance.getPipelineDop() >= 1,
                                "adjusted instance DOP must never drop below 1");
                        Assertions.assertTrue(
                                instance.getPipelineDop() <= dopByWorker.getOrDefault(IDLE_WORKER, 4),
                                "loaded worker instance DOP must not exceed idle worker instances");
                        if (instance.getPipelineDop() < 4) {
                            adjustedSeen = true;
                        }
                    }
                } else {
                    // non-eligible fragment: all instances keep fragment-level consistency
                    List<Integer> dops = Lists.newArrayList();
                    for (FragmentInstance instance : execFragment.getInstances()) {
                        dops.add(instance.getPipelineDop());
                    }
                    Assertions.assertEquals(1, dops.stream().distinct().count(),
                            "fragment with upstream input must keep consistent instance DOPs");
                }
            }
            Assertions.assertTrue(scanFragmentSeen, "expected an eligible scan fragment");
            Assertions.assertTrue(adjustedSeen,
                    "expected the loaded worker instance to be downscaled; dops=" + dumpDops(dag));
        } finally {
            connectContext.getSessionVariable().setEnableAutoDop(false);
        }
    }

    @Test
    public void testAdjustmentDisabledKeepsBaseline() throws Exception {
        ConfigBase.setMutableConfig("auto_dop_instance_adjust_enabled", "false", false, "");
        AutoDopController.setClientForTest(request -> {
            resourceView.putLoadForTest(LOADED_WORKER, 0.95, 0.95, true);
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = request.queryId;
            for (AutoDopRequest.FragmentInfo fragment : request.fragments) {
                response.decisions.add(new AutoDopResponse.Decision(fragment.fragmentId, 4, "test"));
            }
            return response;
        });
        connectContext.getSessionVariable().setEnableAutoDop(true);
        connectContext.getSessionVariable().setEnablePipelineEngine(true);
        connectContext.getSessionVariable().setEnableRuntimeAdaptiveDop(false);
        connectContext.setExecutionId(new TUniqueId(0x52, 0x0));

        try {
            ExecPlan execPlan = getExecPlan("select count(*) from lineorder_new_l");
            CoordinatorPreprocessor prepare = new CoordinatorPreprocessor(
                    execPlan.getFragments(), execPlan.getScanNodes(), connectContext);
            prepare.computeFragmentInstances();

            ExecutionDAG dag = prepare.getExecutionDAG();
            for (ExecutionFragment execFragment : dag.getFragmentsInCreatedOrder()) {
                for (FragmentInstance instance : execFragment.getInstances()) {
                    Assertions.assertEquals(4, instance.getPipelineDop());
                }
            }
        } finally {
            connectContext.getSessionVariable().setEnableAutoDop(false);
            ConfigBase.setMutableConfig("auto_dop_instance_adjust_enabled", "true", false, "");
        }
    }

    @Test
    public void testTraceThreeStageSnapshots() throws Exception {
        AutoDopController.setClientForTest(request -> {
            resourceView.putLoadForTest(LOADED_WORKER, 0.9, 0.9, true);
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = request.queryId;
            for (AutoDopRequest.FragmentInfo fragment : request.fragments) {
                response.decisions.add(new AutoDopResponse.Decision(fragment.fragmentId, 4, "test"));
            }
            return response;
        });
        connectContext.getSessionVariable().setEnableAutoDop(true);
        connectContext.getSessionVariable().setEnablePipelineEngine(true);
        connectContext.getSessionVariable().setEnableRuntimeAdaptiveDop(false);
        connectContext.setExecutionId(new TUniqueId(0x53, 0x0));

        try {
            ExecPlan execPlan = getExecPlan("select count(*) from lineorder_new_l group by P_SIZE");
            CoordinatorPreprocessor prepare = new CoordinatorPreprocessor(
                    execPlan.getFragments(), execPlan.getScanNodes(), connectContext);
            prepare.computeFragmentInstances();

            // computeFragmentInstances already captured the final snapshot via captureActualDop;
            // read the three-stage snapshots from the trace record.
            List<AutoDopTraceManager.ActualFragmentDop> fragments =
                    AutoDopTraceManager.traceFragmentsForTest(connectContext.getExecutionId());
            Assertions.assertNotNull(fragments);
            Assertions.assertFalse(fragments.isEmpty());
            for (AutoDopTraceManager.ActualFragmentDop fragment : fragments) {
                Assertions.assertNotNull(fragment.baselineInstanceDops,
                        "baseline snapshot must be recorded");
                Assertions.assertNotNull(fragment.adjustedInstanceDops,
                        "adjusted snapshot must be recorded");
                Assertions.assertFalse(fragment.instanceDops.isEmpty(),
                        "final snapshot must be recorded");
                Assertions.assertEquals(fragment.baselineInstanceDops.size(), fragment.instanceDops.size());
                Assertions.assertEquals(fragment.adjustedInstanceDops.size(), fragment.instanceDops.size());
                for (int i = 0; i < fragment.instanceDops.size(); i++) {
                    Assertions.assertTrue(
                            fragment.baselineInstanceDops.get(i) >= fragment.adjustedInstanceDops.get(i),
                            "baseline >= adjusted for every instance");
                    Assertions.assertTrue(
                            fragment.adjustedInstanceDops.get(i) >= fragment.instanceDops.get(i),
                            "adjusted >= final for every instance");
                }
            }
        } finally {
            connectContext.getSessionVariable().setEnableAutoDop(false);
        }
    }

    private static String dumpDops(ExecutionDAG dag) {
        StringBuilder sb = new StringBuilder();
        for (ExecutionFragment execFragment : dag.getFragmentsInCreatedOrder()) {
            sb.append(execFragment.getFragmentId()).append('[');
            for (FragmentInstance instance : execFragment.getInstances()) {
                sb.append(instance.getWorkerId()).append('=').append(instance.getPipelineDop()).append(',');
            }
            sb.append("];");
        }
        return sb.toString();
    }
}
