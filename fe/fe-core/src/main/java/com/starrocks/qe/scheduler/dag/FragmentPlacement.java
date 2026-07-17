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

package com.starrocks.qe.scheduler.dag;

import com.google.common.collect.Lists;
import com.google.common.collect.Sets;

import java.util.List;
import java.util.Set;

public class FragmentPlacement {
    private final List<Long> workerIds = Lists.newArrayList();
    private final Set<Integer> replicatedScanIds = Sets.newHashSet();

    public List<Long> getWorkerIds() {
        return workerIds;
    }

    public Set<Integer> getReplicatedScanIds() {
        return replicatedScanIds;
    }

    public void addWorker(long workerId) {
        workerIds.add(workerId);
    }

    public void clear() {
        workerIds.clear();
        replicatedScanIds.clear();
    }
}
