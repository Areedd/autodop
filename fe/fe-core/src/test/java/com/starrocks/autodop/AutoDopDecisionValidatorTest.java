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

import com.starrocks.common.StarRocksException;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.Map;

public class AutoDopDecisionValidatorTest {
    @Test
    public void testValidateDecision() throws Exception {
        Map<String, Integer> decisions = AutoDopDecisionValidator.validateOrThrow(
                request(), response("q1", new AutoDopResponse.Decision("F01", 4, "test")));

        Assertions.assertEquals(4, decisions.get("F01"));
    }

    @Test
    public void testRejectInvalidDecision() {
        Assertions.assertThrows(StarRocksException.class, () -> AutoDopDecisionValidator.validateOrThrow(
                request(), response("other", new AutoDopResponse.Decision("F01", 4, "test"))));
        Assertions.assertThrows(StarRocksException.class, () -> AutoDopDecisionValidator.validateOrThrow(
                request(), response("q1", new AutoDopResponse.Decision("F09", 4, "test"))));
        Assertions.assertThrows(StarRocksException.class, () -> AutoDopDecisionValidator.validateOrThrow(
                request(), response("q1", new AutoDopResponse.Decision("F01", 3, "test"))));
        Assertions.assertThrows(StarRocksException.class, () -> AutoDopDecisionValidator.validateOrThrow(
                request(), response("q1")));
    }

    private AutoDopRequest request() {
        AutoDopRequest request = new AutoDopRequest();
        request.queryId = "q1";
        AutoDopRequest.FragmentInfo fragment = new AutoDopRequest.FragmentInfo();
        fragment.fragmentId = "F01";
        fragment.candidateDops = Arrays.asList(1, 2, 4, 8);
        request.fragments.add(fragment);
        return request;
    }

    private AutoDopResponse response(String queryId, AutoDopResponse.Decision... decisions) {
        AutoDopResponse response = new AutoDopResponse();
        response.queryId = queryId;
        response.decisions = Arrays.asList(decisions);
        return response;
    }
}
