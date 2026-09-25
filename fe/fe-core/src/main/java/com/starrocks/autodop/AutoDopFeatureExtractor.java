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

import com.google.common.base.Preconditions;
import com.google.common.collect.Lists;
import com.starrocks.common.util.DebugUtil;
import com.starrocks.planner.DataPartition;
import com.starrocks.planner.DataSink;
import com.starrocks.planner.DataStreamSink;
import com.starrocks.planner.ExchangeNode;
import com.starrocks.planner.JoinNode;
import com.starrocks.planner.MultiCastDataSink;
import com.starrocks.planner.MultiCastPlanFragment;
import com.starrocks.planner.PlanFragment;
import com.starrocks.planner.PlanNode;
import com.starrocks.planner.SplitCastDataSink;
import com.starrocks.planner.SplitCastPlanFragment;
import com.starrocks.qe.ConnectContext;
import com.starrocks.qe.SessionVariable;
import com.starrocks.qe.scheduler.dag.ExecutionDAG;
import com.starrocks.qe.scheduler.dag.ExecutionFragment;
import com.starrocks.qe.scheduler.dag.JobSpec;
import com.starrocks.thrift.TScanRangeParams;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

public final class AutoDopFeatureExtractor {
    private AutoDopFeatureExtractor() {
    }

    public static AutoDopRequest extract(ConnectContext context, JobSpec jobSpec, ExecutionDAG executionDAG,
                                         Map<String, Integer> originalDop) {
        AutoDopRequest request = new AutoDopRequest();
        request.queryId = DebugUtil.printId(jobSpec.getQueryId());
        fillSessionInfo(request, context, jobSpec);

        for (ExecutionFragment execFragment : executionDAG.getFragmentsInPreorder()) {
            request.fragments.add(extractFragment(execFragment, executionDAG, request.session, originalDop));
        }
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInPreorder()) {
            request.edges.addAll(extractEdges(execFragment.getPlanFragment(), originalDop));
        }
        fillWorkerLoads(request, executionDAG);
        return request;
    }

    private static void fillWorkerLoads(AutoDopRequest request, ExecutionDAG executionDAG) {
        LinkedHashSet<Long> workerIds = new LinkedHashSet<>();
        for (ExecutionFragment execFragment : executionDAG.getFragmentsInPreorder()) {
            workerIds.addAll(execFragment.getPlacement().getWorkerIds());
        }
        AutoDopResourceView.fillWorkerLoads(request, workerIds);
    }

    private static void fillSessionInfo(AutoDopRequest request, ConnectContext context, JobSpec jobSpec) {
        request.session.pipelineEnabled = jobSpec.isEnablePipeline();
        request.session.warehouseId = jobSpec.getWarehouseId();
        if (context == null) {
            return;
        }

        SessionVariable sessionVariable = context.getSessionVariable();
        request.session.pipelineDop = sessionVariable.getPipelineDop();
        request.session.degreeOfParallelism = sessionVariable.getDegreeOfParallelism();
        request.session.maxPipelineDop = sessionVariable.getMaxPipelineDop();
        request.session.autoDopMaxDop = sessionVariable.getAutoDopMaxDop();
        request.session.enableRuntimeAdaptiveDop = sessionVariable.isEnableRuntimeAdaptiveDop();
    }

    private static AutoDopRequest.FragmentInfo extractFragment(ExecutionFragment execFragment, ExecutionDAG executionDAG,
                                                               AutoDopRequest.SessionInfo sessionInfo,
                                                               Map<String, Integer> originalDop) {
        PlanFragment fragment = execFragment.getPlanFragment();
        String fragmentId = fragment.getFragmentId().toString();
        PlanNode root = fragment.getPlanRoot();
        PlanNode leftMost = root == null ? null : fragment.getLeftMostNode();

        AutoDopRequest.FragmentInfo info = new AutoDopRequest.FragmentInfo();
        info.fragmentId = fragmentId;
        info.fragmentIndex = execFragment.getFragmentIndex();
        info.originalDop = originalDop.getOrDefault(fragmentId, fragment.getPipelineDop());
        info.candidateDops = candidateDops(info.originalDop, sessionInfo);
        info.parallelExecNum = fragment.getParallelExecNum();
        info.rootNodeId = root == null ? -1 : root.getId().asInt();
        info.rootNode = root == null ? "" : root.getPlanNodeName();
        info.leftmostNodeId = leftMost == null ? -1 : leftMost.getId().asInt();
        info.leftmostNode = leftMost == null ? "" : leftMost.getPlanNodeName();
        info.operatorCounts.putAll(operatorCounts(root));
        info.operatorSignature = operatorSignature(info.operatorCounts);
        info.dataPartition = fragment.getDataPartition().getType().name();
        info.outputPartition = fragment.getOutputPartition().getType().name();
        info.sinkType = fragment.getSink() == null ? "" : fragment.getSink().getClass().getSimpleName();
        info.joinType = firstJoinType(root);
        info.fragmentCost = fragment.getFragmentCost();
        info.estimatedCardinality = root == null ? -1 : root.getCardinality();
        info.estimatedAvgRowSize = root == null ? 0 : root.getAvgRowSize();
        info.scanNodeCount = execFragment.getScanNodes().size();
        info.scanRangeCount = scanRangeCount(execFragment);
        info.instanceCount = execFragment.getPlacement().getWorkerIds().size();
        info.workerIds = workerIds(execFragment);
        info.colocated = root != null && execFragment.isColocated();
        info.replicated = root != null && execFragment.isReplicated();
        info.bucketShuffle = root != null && execFragment.isLocalBucketShuffleJoin();
        info.gatherRoot = executionDAG.getRootFragment() == execFragment && fragment.isGatherFragment();
        return info;
    }

    static Map<String, Integer> operatorCounts(PlanNode root) {
        Map<String, Integer> counts = new TreeMap<>();
        collectOperatorCounts(root, counts);
        return counts;
    }

    private static void collectOperatorCounts(PlanNode node, Map<String, Integer> counts) {
        if (node == null) {
            return;
        }
        counts.merge(node.getPlanNodeName(), 1, Integer::sum);
        if (node instanceof ExchangeNode) {
            return;
        }
        for (PlanNode child : node.getChildren()) {
            collectOperatorCounts(child, counts);
        }
    }

    private static String firstJoinType(PlanNode node) {
        if (node == null || node instanceof ExchangeNode) {
            return "";
        }
        if (node instanceof JoinNode) {
            return ((JoinNode) node).getJoinOp().name();
        }
        for (PlanNode child : node.getChildren()) {
            String joinType = firstJoinType(child);
            if (!joinType.isEmpty()) {
                return joinType;
            }
        }
        return "";
    }

    private static String operatorSignature(Map<String, Integer> counts) {
        StringBuilder signature = new StringBuilder();
        counts.forEach((name, count) -> {
            if (signature.length() > 0) {
                signature.append('|');
            }
            signature.append(name).append('=').append(count);
        });
        return signature.toString();
    }

    static List<AutoDopRequest.EdgeInfo> extractEdges(PlanFragment fragment, Map<String, Integer> originalDop) {
        if (fragment instanceof MultiCastPlanFragment) {
            MultiCastPlanFragment multiCast = (MultiCastPlanFragment) fragment;
            MultiCastDataSink sink = (MultiCastDataSink) multiCast.getSink();
            return extractFanOutEdges(fragment, multiCast.getDestFragmentList(), sink.getDataStreamSinks(), originalDop);
        }
        if (fragment instanceof SplitCastPlanFragment) {
            SplitCastPlanFragment splitCast = (SplitCastPlanFragment) fragment;
            SplitCastDataSink sink = (SplitCastDataSink) splitCast.getSink();
            return extractFanOutEdges(fragment, splitCast.getDestFragmentList(), sink.getDataStreamSinks(), originalDop);
        }

        PlanFragment destFragment = fragment.getDestFragment();
        if (destFragment == null) {
            return Lists.newArrayList();
        }
        return Lists.newArrayList(extractEdge(fragment, destFragment, fragment.getSink(),
                fragment.getOutputPartition(), originalDop));
    }

    private static List<AutoDopRequest.EdgeInfo> extractFanOutEdges(
            PlanFragment sourceFragment, List<PlanFragment> destFragments, List<DataStreamSink> sinks,
            Map<String, Integer> originalDop) {
        Preconditions.checkState(destFragments.size() == sinks.size(),
                "fan-out fragment destinations and sinks must have the same size");
        List<AutoDopRequest.EdgeInfo> edges = Lists.newArrayList();
        for (int i = 0; i < destFragments.size(); i++) {
            DataStreamSink sink = sinks.get(i);
            edges.add(extractEdge(sourceFragment, destFragments.get(i), sink, sink.getOutputPartition(), originalDop));
        }
        return edges;
    }

    private static AutoDopRequest.EdgeInfo extractEdge(
            PlanFragment sourceFragment, PlanFragment destFragment, DataSink sink, DataPartition outputPartition,
            Map<String, Integer> originalDop) {
        AutoDopRequest.EdgeInfo edge = new AutoDopRequest.EdgeInfo();
        edge.sourceFragmentId = sourceFragment.getFragmentId().toString();
        edge.destFragmentId = destFragment.getFragmentId().toString();
        edge.exchangeNodeId = sink == null || sink.getExchNodeId() == null ? -1 : sink.getExchNodeId().asInt();
        edge.partitionType = outputPartition.getType().name();
        edge.destOriginalDop = originalDop.getOrDefault(edge.destFragmentId, destFragment.getPipelineDop());
        return edge;
    }

    static List<Integer> candidateDops(int originalDop, AutoDopRequest.SessionInfo sessionInfo) {
        int maxDop = 32;
        if (sessionInfo != null) {
            if (sessionInfo.autoDopMaxDop > 0) {
                maxDop = sessionInfo.autoDopMaxDop;
            } else if (sessionInfo.maxPipelineDop > 0) {
                maxDop = sessionInfo.maxPipelineDop;
            } else if (sessionInfo.degreeOfParallelism > 0) {
                maxDop = sessionInfo.degreeOfParallelism;
            }
        }
        maxDop = Math.max(1, Math.min(32, maxDop));

        List<Integer> candidates = Lists.newArrayList();
        for (int dop = 1; dop <= maxDop; dop <<= 1) {
            candidates.add(dop);
        }
        return candidates;
    }

    private static int scanRangeCount(ExecutionFragment execFragment) {
        int count = 0;
        for (Map<Integer, List<TScanRangeParams>> scanRangesPerNode :
                execFragment.getScanRangeAssignment().values()) {
            for (List<TScanRangeParams> scanRanges : scanRangesPerNode.values()) {
                count += scanRanges.size();
            }
        }
        return count;
    }

    private static List<Long> workerIds(ExecutionFragment execFragment) {
        return Lists.newArrayList(new LinkedHashSet<>(execFragment.getPlacement().getWorkerIds()));
    }
}
