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

import com.starrocks.planner.DataPartition;
import com.starrocks.planner.DataSink;
import com.starrocks.planner.DataStreamSink;
import com.starrocks.planner.ExchangeNode;
import com.starrocks.planner.MultiCastDataSink;
import com.starrocks.planner.MultiCastPlanFragment;
import com.starrocks.planner.PlanFragment;
import com.starrocks.planner.PlanFragmentId;
import com.starrocks.planner.PlanNode;
import com.starrocks.planner.PlanNodeId;
import com.starrocks.planner.SplitCastDataSink;
import com.starrocks.planner.SplitCastPlanFragment;
import com.starrocks.thrift.TPlanNode;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

public class AutoDopFeatureExtractorTest {
    @Test
    public void testOperatorCountsStopAtExchangeBoundary() {
        TestPlanNode root = new TestPlanNode(1, "ROOT");
        TestPlanNode localChild = new TestPlanNode(2, "LOCAL");
        TestPlanNode remoteChild = new TestPlanNode(3, "REMOTE");
        new PlanFragment(new PlanFragmentId(2), remoteChild, DataPartition.RANDOM);
        ExchangeNode exchange = new ExchangeNode(new PlanNodeId(4), remoteChild, DataPartition.RANDOM);
        root.addChild(localChild);
        root.addChild(exchange);

        Assertions.assertEquals(
                Map.of("ROOT", 1, "LOCAL", 1, "EXCHANGE", 1),
                AutoDopFeatureExtractor.operatorCounts(root));
    }

    private static class TestPlanNode extends PlanNode {
        private TestPlanNode(int id, String name) {
            super(new PlanNodeId(id), name);
        }

        @Override
        protected void toThrift(TPlanNode msg) {
        }
    }

    @Test
    public void testExtractMultiCastEdges() {
        MultiCastDataSink sink = new MultiCastDataSink();
        List<PlanFragment> destinations = destinations();
        sink.getDataStreamSinks().add(streamSink(11, DataPartition.RANDOM));
        sink.getDataStreamSinks().add(streamSink(12, DataPartition.HYBRID_HASH_PARTITIONED));
        MultiCastPlanFragment source = new TestMultiCastPlanFragment(destinations, sink);

        List<AutoDopRequest.EdgeInfo> edges = AutoDopFeatureExtractor.extractEdges(
                source, Map.of("F02", 2, "F03", 8));

        assertEdges(edges);
    }

    @Test
    public void testExtractSplitCastEdges() {
        SplitCastDataSink sink = new SplitCastDataSink();
        List<PlanFragment> destinations = destinations();
        sink.getDataStreamSinks().add(streamSink(11, DataPartition.RANDOM));
        sink.getDataStreamSinks().add(streamSink(12, DataPartition.HYBRID_HASH_PARTITIONED));
        SplitCastPlanFragment source = new TestSplitCastPlanFragment(destinations, sink);

        List<AutoDopRequest.EdgeInfo> edges = AutoDopFeatureExtractor.extractEdges(
                source, Map.of("F02", 2, "F03", 8));

        assertEdges(edges);
    }

    private List<PlanFragment> destinations() {
        return List.of(
                new PlanFragment(new PlanFragmentId(2), null, DataPartition.UNPARTITIONED),
                new PlanFragment(new PlanFragmentId(3), null, DataPartition.UNPARTITIONED));
    }

    private DataStreamSink streamSink(int exchangeNodeId, DataPartition partition) {
        DataStreamSink sink = new DataStreamSink(new PlanNodeId(exchangeNodeId));
        sink.setPartition(partition);
        return sink;
    }

    private void assertEdges(List<AutoDopRequest.EdgeInfo> edges) {
        Assertions.assertEquals(2, edges.size());
        Assertions.assertEquals("F01", edges.get(0).sourceFragmentId);
        Assertions.assertEquals("F02", edges.get(0).destFragmentId);
        Assertions.assertEquals(11, edges.get(0).exchangeNodeId);
        Assertions.assertEquals("RANDOM", edges.get(0).partitionType);
        Assertions.assertEquals(2, edges.get(0).destOriginalDop);
        Assertions.assertEquals("F03", edges.get(1).destFragmentId);
        Assertions.assertEquals(12, edges.get(1).exchangeNodeId);
        Assertions.assertEquals("HYBRID_HASH_PARTITIONED", edges.get(1).partitionType);
        Assertions.assertEquals(8, edges.get(1).destOriginalDop);
    }

    private static class TestMultiCastPlanFragment extends MultiCastPlanFragment {
        private final List<PlanFragment> destinations;
        private final MultiCastDataSink sink;

        private TestMultiCastPlanFragment(List<PlanFragment> destinations, MultiCastDataSink sink) {
            super(new PlanFragment(new PlanFragmentId(1), null, DataPartition.UNPARTITIONED));
            this.destinations = destinations;
            this.sink = sink;
        }

        @Override
        public List<PlanFragment> getDestFragmentList() {
            return destinations;
        }

        @Override
        public DataSink getSink() {
            return sink;
        }
    }

    private static class TestSplitCastPlanFragment extends SplitCastPlanFragment {
        private final List<PlanFragment> destinations;
        private final SplitCastDataSink sink;

        private TestSplitCastPlanFragment(List<PlanFragment> destinations, SplitCastDataSink sink) {
            super(new PlanFragment(new PlanFragmentId(1), null, DataPartition.UNPARTITIONED));
            this.destinations = destinations;
            this.sink = sink;
        }

        @Override
        public List<PlanFragment> getDestFragmentList() {
            return destinations;
        }

        @Override
        public DataSink getSink() {
            return sink;
        }
    }
}
