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

import com.starrocks.persist.gson.GsonUtils;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.Map;

public class AutoDopJsonSerdeTest {
    @Test
    public void testRequestKeepsDecisionFieldsWithGsonUtils() {
        AutoDopRequest request = new AutoDopRequest();
        request.queryId = "q1";

        AutoDopRequest.FragmentInfo fragment = new AutoDopRequest.FragmentInfo();
        fragment.fragmentId = "F01";
        fragment.originalDop = 8;
        fragment.candidateDops = Arrays.asList(1, 2, 4, 8);
        fragment.operatorCounts = Map.of("INTERSECT", 1, "AGGREGATE", 2);
        fragment.operatorSignature = "AGGREGATE=2|INTERSECT=1";
        request.fragments.add(fragment);

        String json = GsonUtils.GSON.toJson(request);

        Assertions.assertTrue(json.contains("\"fragments\""));
        Assertions.assertTrue(json.contains("\"fragment_id\":\"F01\""));
        Assertions.assertTrue(json.contains("\"candidate_dops\""));
        Assertions.assertTrue(json.contains("\"operator_counts\""));
        Assertions.assertTrue(json.contains("\"INTERSECT\":1"));
    }

    @Test
    public void testResponseReadsDecisionsWithGsonUtils() {
        String json = "{\"version\":1,\"query_id\":\"q1\",\"confidence\":0.92,\"decisions\":[{\"fragment_id\":\"F01\","
                + "\"dop\":4,\"reason\":\"test\"}]}";

        AutoDopResponse response = GsonUtils.GSON.fromJson(json, AutoDopResponse.class);

        Assertions.assertEquals("q1", response.queryId);
        Assertions.assertEquals(0.92, response.confidence, 1e-9);
        Assertions.assertEquals(1, response.decisions.size());
        Assertions.assertEquals("F01", response.decisions.get(0).fragmentId);
        Assertions.assertEquals(4, response.decisions.get(0).dop);
    }

    @Test
    public void testResponseDoesNotDefaultMissingConfidence() {
        String json = "{\"version\":1,\"query_id\":\"q1\",\"decisions\":[]}";

        AutoDopResponse response = GsonUtils.GSON.fromJson(json, AutoDopResponse.class);

        Assertions.assertNull(response.confidence);
    }
}
