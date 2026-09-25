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

#include "util/cpu_usage_info.h"

#include <gtest/gtest.h>

#include "testutil/parallel_test.h"
#include "util/cpu_info.h"

namespace starrocks {

class CpuUsageRecorderTestPeer {
public:
    static void set_one_second_usage(CpuUsageRecorder& recorder, int permille) {
        recorder._curr_idx = 1;
        recorder._timestamp[0] = 0;
        recorder._timestamp[1] = 1'000'000'000;
        recorder._proc_time[0] = 0;
        recorder._proc_time[1] =
                static_cast<uint64_t>(CpuInfo::num_cores()) * recorder.SECOND_CLOCK_TICK * permille / 1000;
    }
};

PARALLEL_TEST(CpuUsageRecorderTest, available_cpu_capacity) {
    CpuUsageRecorder recorder;
    for (int expected : {0, 500, 1000}) {
        CpuUsageRecorderTestPeer::set_one_second_usage(recorder, expected);
        ASSERT_EQ(expected, recorder.cpu_used_permille());
    }
}

PARALLEL_TEST(CpuUsageRecorderTest, normal) {
    CpuUsageRecorder recorder;
    ASSERT_EQ(0, recorder.cpu_used_permille());

    recorder.update_interval();
    int cpu_used_permille = recorder.cpu_used_permille();
    ASSERT_GE(cpu_used_permille, 0);
}

} // namespace starrocks
