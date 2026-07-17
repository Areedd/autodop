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

import com.google.gson.annotations.SerializedName;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class AutoDopRequest {
    @SerializedName("version")
    public int version = 1;

    @SerializedName("query_id")
    public String queryId = "";

    @SerializedName("session")
    public SessionInfo session = new SessionInfo();

    @SerializedName("fragments")
    public List<FragmentInfo> fragments = new ArrayList<>();

    @SerializedName("edges")
    public List<EdgeInfo> edges = new ArrayList<>();

    public static class SessionInfo {
        @SerializedName("pipeline_enabled")
        public boolean pipelineEnabled;

        @SerializedName("pipeline_dop")
        public int pipelineDop;

        @SerializedName("degree_of_parallelism")
        public int degreeOfParallelism;

        @SerializedName("max_pipeline_dop")
        public int maxPipelineDop;

        @SerializedName("auto_dop_max_dop")
        public int autoDopMaxDop;

        @SerializedName("enable_runtime_adaptive_dop")
        public boolean enableRuntimeAdaptiveDop;

        @SerializedName("warehouse_id")
        public long warehouseId;
    }

    public static class FragmentInfo {
        @SerializedName("fragment_id")
        public String fragmentId;

        @SerializedName("fragment_index")
        public int fragmentIndex;

        @SerializedName("original_dop")
        public int originalDop;

        @SerializedName("candidate_dops")
        public List<Integer> candidateDops = new ArrayList<>();

        @SerializedName("parallel_exec_num")
        public int parallelExecNum;

        @SerializedName("root_node_id")
        public int rootNodeId;

        @SerializedName("root_node")
        public String rootNode = "";

        @SerializedName("leftmost_node_id")
        public int leftmostNodeId;

        @SerializedName("leftmost_node")
        public String leftmostNode = "";

        @SerializedName("operator_counts")
        public Map<String, Integer> operatorCounts = new LinkedHashMap<>();

        @SerializedName("operator_signature")
        public String operatorSignature = "";

        @SerializedName("data_partition")
        public String dataPartition = "";

        @SerializedName("output_partition")
        public String outputPartition = "";

        @SerializedName("sink_type")
        public String sinkType = "";

        @SerializedName("fragment_cost")
        public double fragmentCost;

        @SerializedName("estimated_cardinality")
        public long estimatedCardinality;

        @SerializedName("estimated_avg_row_size")
        public float estimatedAvgRowSize;

        @SerializedName("scan_node_count")
        public int scanNodeCount;

        @SerializedName("scan_range_count")
        public int scanRangeCount;

        @SerializedName("instance_count")
        public int instanceCount;

        @SerializedName("worker_ids")
        public List<Long> workerIds = new ArrayList<>();

        @SerializedName("is_colocated")
        public boolean colocated;

        @SerializedName("is_replicated")
        public boolean replicated;

        @SerializedName("is_bucket_shuffle")
        public boolean bucketShuffle;

        @SerializedName("is_gather_root")
        public boolean gatherRoot;
    }

    public static class EdgeInfo {
        @SerializedName("source_fragment_id")
        public String sourceFragmentId = "";

        @SerializedName("dest_fragment_id")
        public String destFragmentId = "";

        @SerializedName("exchange_node_id")
        public int exchangeNodeId = -1;

        @SerializedName("partition_type")
        public String partitionType = "";

        @SerializedName("dest_original_dop")
        public int destOriginalDop;
    }
}
