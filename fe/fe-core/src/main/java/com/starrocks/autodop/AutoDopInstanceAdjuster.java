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
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.ExecutionFragment;
import com.starrocks.qe.scheduler.dag.FragmentInstance;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * Instance-level DOP adjustment for scan fragments (DD04 §1.3/§2/§4).
 *
 * <p>For fragments that (1) have no upstream exchange input, (2) are not part of a colocate
 * topology and (3) are not part of a bucket shuffle topology, the per-instance DOP is scaled
 * down on BEs whose smoothed resource usage exceeds the dead-zone thresholds. Instances on
 * lightly loaded BEs keep the baseline DOP (adjustment only ever lowers parallelism), the
 * result is rounded to the nearest candidate at or below the scaled value and never drops
 * below 1, and all instances on the same BE share the same factor.
 *
 * <p>The adjustment runs inside the decision window, after instances are materialized and
 * before the DAG is finalized: the instance count and the fragment-level pipeline DOP stay
 * unchanged, so the exchange routing (sender count = instance count, exchDop = destination
 * fragment DOP) is not affected.
 */
public final class AutoDopInstanceAdjuster {
    private static final Logger LOG = LogManager.getLogger(AutoDopInstanceAdjuster.class);

    private AutoDopInstanceAdjuster() {
    }

    public static void adjust(ExecutionDAG executionDAG, AutoDopResult autoDopResult) {
        if (!Config.auto_dop_instance_adjust_enabled || executionDAG == null) {
            return;
        }

        Set<String> destFragmentIds = destFragmentIds(executionDAG);
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInPreorder()) {
            String fragmentId = execFragment.getFragmentId().toString();
            if (destFragmentIds.contains(fragmentId)) {
                // has upstream data exchange input: instance DOP keeps fragment-level consistency
                continue;
            }
            if (execFragment.isColocated() || execFragment.isLocalBucketShuffleJoin()) {
                // colocate / bucket shuffle topologies require aligned per-instance parallelism
                continue;
            }
            adjustFragment(execFragment, fragmentId, autoDopResult);
        }
    }

    private static Set<String> destFragmentIds(ExecutionDAG executionDAG) {
        Set<String> destFragmentIds = new HashSet<>();
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInPreorder()) {
            for (AutoDopRequest.EdgeInfo edge :
                    AutoDopFeatureExtractor.extractEdges(execFragment.getPlanFragment(), Collections.emptyMap())) {
                destFragmentIds.add(edge.destFragmentId);
            }
        }
        return destFragmentIds;
    }

    private static void adjustFragment(ExecutionFragment execFragment, String fragmentId,
                                       AutoDopResult autoDopResult) {
        List<Integer> candidateDops = candidateDops(execFragment, fragmentId, autoDopResult);
        for (FragmentInstance instance : execFragment.getInstances()) {
            Optional<AutoDopResourceView.WorkerLoad> load =
                    AutoDopResourceView.effectiveInstance().getWorkerLoad(instance.getWorkerId());
            if (!load.isPresent() || !load.get().fresh) {
                // missing or stale resource data: keep the baseline DOP
                continue;
            }
            double factor = instanceFactor(load.get());
            int baseDop = instance.getPipelineDop();
            int adjustedDop = correctedDop(baseDop, factor, candidateDops);
            if (adjustedDop < baseDop) {
                instance.setPipelineDop(adjustedDop);
            }
        }
    }

    /**
     * Per-worker scaling factor with the same dead-zone semantics as the fragment-level load
     * correction (DD03 §4.2), but with a more conservative floor
     * ({@code auto_dop_instance_adjust_floor}, default 0.5).
     */
    static double instanceFactor(AutoDopResourceView.WorkerLoad load) {
        double cpuFactor = dimensionFactor(load.cpuUsed, Config.auto_dop_load_cpu_threshold,
                Config.auto_dop_instance_adjust_floor);
        double memFactor = dimensionFactor(load.memUsedPct, Config.auto_dop_load_mem_threshold,
                Config.auto_dop_instance_adjust_floor);
        return Math.min(cpuFactor, memFactor);
    }

    static double dimensionFactor(double value, double threshold, double floor) {
        if (value <= threshold) {
            return 1.0;
        }
        return Math.max(floor, 1.0 - (value - threshold) / (1.0 - threshold) * (1.0 - floor));
    }

    /**
     * Rounds {@code baseDop * factor} to the closest candidate at or below the scaled value;
     * never raises the DOP and never returns a value below 1.
     */
    static int correctedDop(int baseDop, double factor, List<Integer> candidateDops) {
        if (factor >= 1.0) {
            return baseDop;
        }
        int target = (int) Math.round(baseDop * factor);
        int chosen = -1;
        for (int dop : candidateDops) {
            if (dop <= target) {
                chosen = dop;
            } else {
                break;
            }
        }
        if (chosen < 0) {
            int minCandidate = 1;
            for (int dop : candidateDops) {
                minCandidate = Math.min(minCandidate, dop);
            }
            chosen = Math.max(1, minCandidate);
        }
        return chosen;
    }

    private static List<Integer> candidateDops(ExecutionFragment execFragment, String fragmentId,
                                               AutoDopResult autoDopResult) {
        // Prefer the candidate set of the decision request (derived from the session's
        // auto_dop_max_dop), so the instance adjustment rounds inside the same set as the
        // fragment-level decision. Fall back to powers of two up to 32 defensively.
        if (autoDopResult != null && autoDopResult.getRequest() != null) {
            for (AutoDopRequest.FragmentInfo info : autoDopResult.getRequest().fragments) {
                if (info.fragmentId.equals(fragmentId) && !info.candidateDops.isEmpty()) {
                    return info.candidateDops;
                }
            }
        }
        List<Integer> candidates = new java.util.ArrayList<>();
        for (int dop = 1; dop <= 32; dop <<= 1) {
            candidates.add(dop);
        }
        return candidates;
    }
}
