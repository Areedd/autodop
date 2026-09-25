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

import com.starrocks.thrift.TUniqueId;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

class FifoSlotQueueTest {
    private SlotSelectionStrategyV2.SlotContext slot(long id, int capacity) {
        LogicalSlot logical = new LogicalSlot(new TUniqueId(0, id), "fe", 0,
                LogicalSlot.ABSENT_GROUP_ID, capacity, 0, 0, 0, 0, 0);
        return new SlotSelectionStrategyV2.SlotContext(logical);
    }

    @Test
    void olderLargeQueryIsNotOvertakenBySmallerQueries() {
        FifoSlotQueue q = new FifoSlotQueue();
        var large = slot(1, 48);
        var small = slot(2, 24);
        q.add(large);
        q.add(small);
        assertSame(large, q.peak());
        q.add(slot(3, 1));
        assertSame(large, q.peak());
        assertEquals(3, q.size());
        assertSame(large, q.poll());
        assertSame(small, q.poll());
        assertEquals(1, q.poll().getSlot().getNumPhysicalSlots());
        assertTrue(q.isEmpty());
        assertNull(q.peak());
        assertNull(q.poll());
    }

    @Test
    void migrationDuplicatesDoNotMoveOrReplaceTheHead() {
        FifoSlotQueue q = new FifoSlotQueue();
        var first = slot(1, 48);
        var second = slot(2, 24);
        q.add(first);
        q.add(second);
        q.add(slot(1, 48));
        q.add(second);
        assertEquals(2, q.size());
        assertSame(first, q.poll());
        assertSame(second, q.poll());
    }

    @Test
    void cancellationAndReenqueuePreserveArrivalOrder() {
        FifoSlotQueue q = new FifoSlotQueue();
        var first = slot(1, 48);
        var second = slot(2, 24);
        var third = slot(3, 48);
        q.add(first);
        q.add(second);
        q.add(third);
        assertTrue(q.remove(second));
        assertFalse(q.remove(second));
        assertSame(first, q.peak());
        q.add(second);
        assertTrue(q.remove(first));
        assertSame(third, q.poll());
        assertSame(second, q.poll());
    }

    @Test
    void fifoIsOptInAndDefaultRemainsSwrr() {
        assertEquals(QueryQueueOptions.SchedulePolicy.FIFO, QueryQueueOptions.SchedulePolicy.create("fifo"));
        assertEquals(QueryQueueOptions.SchedulePolicy.SWRR, QueryQueueOptions.SchedulePolicy.createDefault());
        assertNull(QueryQueueOptions.SchedulePolicy.create("unknown"));
    }
}
