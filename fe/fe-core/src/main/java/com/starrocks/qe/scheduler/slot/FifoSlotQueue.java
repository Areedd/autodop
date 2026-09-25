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

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * FIFO enqueue order for normal slots. During policy changes, pending slots keep
 * the migration order supplied by the previous strategy. The strategy's separate
 * small-slot lane and resource-capacity checks are unchanged.
 */
class FifoSlotQueue implements SlotScheduleAlgorithm {
    private final Map<TUniqueId, SlotSelectionStrategyV2.SlotContext> slots = new LinkedHashMap<>();

    @Override
    public int size() {
        return slots.size();
    }

    @Override
    public void add(SlotSelectionStrategyV2.SlotContext slotContext) {
        // Policy migration may add an already queued context again.
        slots.putIfAbsent(slotContext.getSlotId(), slotContext);
    }

    @Override
    public boolean remove(SlotSelectionStrategyV2.SlotContext slotContext) {
        return slots.remove(slotContext.getSlotId()) != null;
    }

    @Override
    public SlotSelectionStrategyV2.SlotContext peak() {
        return slots.isEmpty() ? null : slots.values().iterator().next();
    }

    @Override
    public SlotSelectionStrategyV2.SlotContext poll() {
        Iterator<SlotSelectionStrategyV2.SlotContext> iterator = slots.values().iterator();
        if (!iterator.hasNext()) {
            return null;
        }
        SlotSelectionStrategyV2.SlotContext first = iterator.next();
        iterator.remove();
        return first;
    }
}
