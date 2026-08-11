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

import com.starrocks.common.Config;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.List;

/**
 * Unit tests for {@link AutoDopInstanceAdjuster} adjustment rules (DD04 §6.3).
 * Integration-level routing-safety tests live in the {@code com.starrocks.qe} package where the
 * {@code CoordinatorPreprocessor} test seam is accessible.
 */
public class AutoDopInstanceAdjusterTest {
    private AutoDopResourceView resourceView;

    @BeforeEach
    public void setUp() {
        resourceView = AutoDopResourceView.newInstanceForTest();
        AutoDopResourceView.setInstanceForTest(resourceView);
        Config.auto_dop_instance_adjust_enabled = true;
        Config.auto_dop_instance_adjust_floor = 0.5;
        Config.auto_dop_load_cpu_threshold = 0.4;
        Config.auto_dop_load_mem_threshold = 0.65;
        Config.auto_dop_load_ewma_alpha = 0.25;
    }

    @AfterEach
    public void tearDown() {
        resourceView.shutdown();
        AutoDopResourceView.resetInstanceForTest();
    }

    @Test
    public void testDimensionFactorDeadZoneAndFloor() {
        Assertions.assertEquals(1.0, AutoDopInstanceAdjuster.dimensionFactor(0.3, 0.4, 0.5), 1e-9);
        Assertions.assertEquals(1.0, AutoDopInstanceAdjuster.dimensionFactor(0.4, 0.4, 0.5), 1e-9);
        // linear drop: 1 - (0.9 - 0.4) / 0.6 * (1 - 0.5) = 1 - 0.4167
        Assertions.assertEquals(1.0 - 0.5 / 0.6 * 0.5,
                AutoDopInstanceAdjuster.dimensionFactor(0.9, 0.4, 0.5), 1e-9);
        Assertions.assertEquals(0.5, AutoDopInstanceAdjuster.dimensionFactor(1.0, 0.4, 0.5), 1e-9);
    }

    @Test
    public void testInstanceFactorTakesMinDimension() {
        AutoDopResourceView.WorkerLoad stressed = new AutoDopResourceView.WorkerLoad(0.9, 0.9, true);
        double cpuFactor = AutoDopInstanceAdjuster.dimensionFactor(0.9, 0.4, 0.5);
        double memFactor = AutoDopInstanceAdjuster.dimensionFactor(0.9, 0.65, 0.5);
        Assertions.assertEquals(Math.min(cpuFactor, memFactor),
                AutoDopInstanceAdjuster.instanceFactor(stressed), 1e-9);

        AutoDopResourceView.WorkerLoad idle = new AutoDopResourceView.WorkerLoad(0.1, 0.1, true);
        Assertions.assertEquals(1.0, AutoDopInstanceAdjuster.instanceFactor(idle), 1e-9);
    }

    @Test
    public void testCorrectedDopRules() {
        List<Integer> candidates = Arrays.asList(1, 2, 4, 8);
        Assertions.assertEquals(4, AutoDopInstanceAdjuster.correctedDop(8, 0.5, candidates));
        Assertions.assertEquals(4, AutoDopInstanceAdjuster.correctedDop(8, 0.75, candidates));
        Assertions.assertEquals(8, AutoDopInstanceAdjuster.correctedDop(8, 1.0, candidates));
        Assertions.assertEquals(1, AutoDopInstanceAdjuster.correctedDop(1, 0.1, candidates));
        Assertions.assertEquals(1, AutoDopInstanceAdjuster.correctedDop(4, 0.01, candidates));
    }
}
