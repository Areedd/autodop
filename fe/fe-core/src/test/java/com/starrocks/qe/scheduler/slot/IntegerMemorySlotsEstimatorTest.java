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

package com.starrocks.qe.scheduler.slot;

import com.starrocks.qe.ConnectContext;
import com.starrocks.qe.DefaultCoordinator;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;

class IntegerMemorySlotsEstimatorTest {
    @Test
    void roundsPartialSlotsUpWithoutPowerOfTwoInflation() {
        assertEquals(21, SlotEstimatorFactory.estimateIntegerSlots(7000, 1000, 3, 96));
        assertEquals(24, SlotEstimatorFactory.estimateIntegerSlots(7001, 1000, 3, 96));
        assertEquals(30, SlotEstimatorFactory.estimateIntegerSlots(10000, 1000, 3, 96));
    }

    @Test
    void clampsBeforeMultiplicationAndHandlesZeroEstimates() {
        assertEquals(96, SlotEstimatorFactory.estimateIntegerSlots(Long.MAX_VALUE, 1, 3, 96));
        assertEquals(95, SlotEstimatorFactory.estimateIntegerSlots(Long.MAX_VALUE, 1, 3, 95));
        assertEquals(3, SlotEstimatorFactory.estimateIntegerSlots(0, 1000, 3, 96));
        assertEquals(3, SlotEstimatorFactory.estimateIntegerSlots(Long.MIN_VALUE, 1000, 3, 96));
        assertEquals(6, SlotEstimatorFactory.estimateIntegerSlots(Long.MAX_VALUE, Long.MAX_VALUE - 1, 3, 96));
    }

    @Test
    void rejectsInvalidCapacity() {
        assertThrows(IllegalArgumentException.class,
                () -> SlotEstimatorFactory.estimateIntegerSlots(1, 0, 3, 96));
        assertThrows(IllegalArgumentException.class,
                () -> SlotEstimatorFactory.estimateIntegerSlots(1, 1000, 0, 96));
        assertThrows(IllegalArgumentException.class,
                () -> SlotEstimatorFactory.estimateIntegerSlots(1, 1000, 3, 0));
    }

    @Test
    void integerPolicyIsOptInAndDefaultRemainsMax() {
        assertEquals(SlotEstimatorFactory.EstimatorPolicy.MAX, SlotEstimatorFactory.EstimatorPolicy.createDefault());
        assertInstanceOf(SlotEstimatorFactory.IntegerMemoryBasedSlotsEstimator.class,
                SlotEstimatorFactory.EstimatorPolicy.create("mbe_ceil").createEstimator());
        assertEquals(SlotEstimatorFactory.MemoryBasedSlotsEstimator.class,
                SlotEstimatorFactory.EstimatorPolicy.MBE.createEstimator().getClass());
    }

    @Test
    void fixedFourBaselineIsIdenticalAndPredictedTenUsesThirtySlots() {
        QueryQueueOptions options = new QueryQueueOptions(true,
                new QueryQueueOptions.V2(4, 3, 8, 12369505812L, 4096, 1));
        long[] cost = {8 * options.v2().getMemBytesPerSlot()};
        var legacy = new SlotEstimatorFactory.MemoryBasedSlotsEstimator() {
            @Override
            protected long getMemoryCost(ConnectContext context, DefaultCoordinator coord) {
                return cost[0];
            }
        };
        var integer = new SlotEstimatorFactory.IntegerMemoryBasedSlotsEstimator() {
            @Override
            protected long getMemoryCost(ConnectContext context, DefaultCoordinator coord) {
                return cost[0];
            }
        };
        assertEquals(24, legacy.estimateSlots(options, null, null));
        assertEquals(24, integer.estimateSlots(options, null, null));
        cost[0] = 10 * options.v2().getMemBytesPerSlot();
        assertEquals(48, legacy.estimateSlots(options, null, null));
        assertEquals(30, integer.estimateSlots(options, null, null));
    }
}
