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

import com.google.common.collect.ImmutableMap;
import com.google.common.collect.Lists;
import com.starrocks.planner.DataPartition;
import com.starrocks.planner.PlanFragment;
import com.starrocks.planner.PlanFragmentId;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

import java.util.List;
public class AutoDopDecisionApplierTest {
    @Test
    public void testApply() {
        PlanFragment fragment = new PlanFragment(new PlanFragmentId(1), null, DataPartition.UNPARTITIONED);
        fragment.setPipelineDop(8);
        List<PlanFragment> fragments = Lists.newArrayList(fragment);

        AutoDopDecisionApplier.apply(fragments, ImmutableMap.of("F01", 4));

        Assertions.assertEquals(4, fragment.getPipelineDop());
    }

    @Test
    public void testCandidateDopsUseFixedDomain() {
        AutoDopRequest.SessionInfo sessionInfo = new AutoDopRequest.SessionInfo();
        sessionInfo.maxPipelineDop = 8;

        List<Integer> candidates = AutoDopFeatureExtractor.candidateDops(6, sessionInfo);

        Assertions.assertEquals(Lists.newArrayList(1, 2, 4, 8), candidates);
    }
}
