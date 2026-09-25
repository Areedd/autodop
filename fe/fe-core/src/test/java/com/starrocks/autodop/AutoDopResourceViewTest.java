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
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class AutoDopResourceViewTest {
    private AutoDopResourceView view;

    @BeforeEach
    public void setUp() {
        view = new AutoDopResourceView();
        AutoDopResourceView.setInstanceForTest(view);
        Config.auto_dop_load_ewma_alpha = 0.25;
        Config.auto_dop_resource_neutral_cpu = 0.0;
        Config.auto_dop_resource_neutral_mem = 0.0;
        Config.auto_dop_resource_fresh_interval_ms = -1;
        Config.auto_dop_capacity_weight = 0.0;
    }

    @AfterEach
    public void tearDown() {
        view.shutdown();
        AutoDopResourceView.resetInstanceForTest();
        Config.auto_dop_resource_neutral_cpu = 0.0;
        Config.auto_dop_resource_neutral_mem = 0.0;
        Config.auto_dop_resource_fresh_interval_ms = -1;
        Config.auto_dop_capacity_weight = 0.0;
    }

    @Test
    public void testFirstSampleTakenAsIs() {
        view.applyRawSample(1, 0.8, 0.9, true);
        Optional<AutoDopResourceView.WorkerLoad> load = view.getWorkerLoad(1);
        assertTrue(load.isPresent());
        assertEquals(0.8, load.get().cpuUsed, 1e-9);
        assertEquals(0.9, load.get().memUsedPct, 1e-9);
        assertTrue(load.get().fresh);
    }

    @Test
    public void testEwmaSmoothing() {
        view.applyRawSample(1, 1.0, 1.0, true);
        // ewma(alpha=0.25): next = 0.25 * raw + 0.75 * previous
        view.applyRawSample(1, 0.0, 0.0, true);
        assertEquals(0.75, view.getWorkerLoad(1).get().cpuUsed, 1e-9);
        assertEquals(0.75, view.getWorkerLoad(1).get().memUsedPct, 1e-9);

        view.applyRawSample(1, 0.0, 0.0, true);
        assertEquals(0.5625, view.getWorkerLoad(1).get().cpuUsed, 1e-9);
        assertTrue(view.getWorkerLoad(1).get().fresh);
    }

    @Test
    public void testStaleSampleMarkedNotFreshAndNeutralized() {
        view.applyRawSample(2, 0.9, 0.8, true);
        // stale report: treated as neutral raw values (0) and marked not fresh
        view.applyRawSample(2, 0.0, 0.0, false);
        Optional<AutoDopResourceView.WorkerLoad> load = view.getWorkerLoad(2);
        assertTrue(load.isPresent());
        assertFalse(load.get().fresh);
        assertEquals(0.9 * 0.75, load.get().cpuUsed, 1e-9);
        assertEquals(0.8 * 0.75, load.get().memUsedPct, 1e-9);
    }

    @Test
    public void testUnknownWorkerReturnsEmpty() {
        assertFalse(view.getWorkerLoad(9999).isPresent());
    }

    @Test
    public void testFillWorkerLoadsOmitsMissingWorkers() {
        view.applyRawSample(10, 0.5, 0.5, true);
        AutoDopRequest request = new AutoDopRequest();
        AutoDopResourceView.fillWorkerLoads(request, Arrays.asList(10L, 11L));
        assertEquals(1, request.workerLoads.size());
        assertTrue(request.workerLoads.containsKey("10"));
        assertEquals(0.5, request.workerLoads.get("10").cpuUsed, 1e-9);
        assertFalse(request.workerLoads.containsKey("11"));
    }

    @Test
    public void testFillWorkerLoadsWithEmptyView() {
        AutoDopRequest request = new AutoDopRequest();
        AutoDopResourceView.fillWorkerLoads(request, Arrays.asList(1L));
        assertTrue(request.workerLoads.isEmpty());
    }

    @Test
    public void testNeutralValuesAreConfigurable() {
        Config.auto_dop_resource_neutral_cpu = 0.3;
        Config.auto_dop_resource_neutral_mem = 0.4;
        // stale report: raw values come from the neutral config instead of a hard-coded 0.0
        view.applyRawSample(3, 0.3, 0.4, false);
        Optional<AutoDopResourceView.WorkerLoad> load = view.getWorkerLoad(3);
        assertTrue(load.isPresent());
        assertFalse(load.get().fresh);
        assertEquals(0.3, load.get().cpuUsed, 1e-9);
        assertEquals(0.4, load.get().memUsedPct, 1e-9);
    }

    @Test
    public void testCustomFreshnessWindow() {
        Config.auto_dop_resource_fresh_interval_ms = 5000;
        view.applyRawSample(4, 0.5, 0.5, true);
        assertTrue(view.getWorkerLoad(4).get().fresh);

        // a stale report no longer refreshes lastSeenFreshMs
        view.applyRawSample(4, 0.0, 0.0, false);
        assertTrue(view.getWorkerLoad(4).get().fresh);

        // time passes beyond the configured window: worker becomes stale
        view.loadsForTest().get(4L).lastSeenFreshMs =
                System.currentTimeMillis() - Config.auto_dop_resource_fresh_interval_ms - 1000;
        assertFalse(view.getWorkerLoad(4).get().fresh);
    }

    @Test
    public void testCustomFreshnessWindowNeverSeenFreshIsStale() {
        Config.auto_dop_resource_fresh_interval_ms = 5000;
        view.applyRawSample(5, 0.0, 0.0, false);
        assertFalse(view.getWorkerLoad(5).get().fresh);
    }

    @Test
    public void testCapacityFactorDiffersForSameUsage() {
        Config.auto_dop_capacity_weight = 0.5;
        view.setReferenceCapacityForTest(16.0, 32L * 1024 * 1024 * 1024);
        // same usage, different capacities: the smaller node gets a higher tension
        view.putLoadForTest(1, 0.5, 0.5, true, 4.0, 8L * 1024 * 1024 * 1024);
        view.putLoadForTest(2, 0.5, 0.5, true, 16.0, 32L * 1024 * 1024 * 1024);
        double small = view.getWorkerLoad(1).get().cpuUsed;
        double large = view.getWorkerLoad(2).get().cpuUsed;
        // 0.5 * (1 + 0.5 * (1 - 4/16)) = 0.6875 vs 0.5 * (1 + 0.5 * 0) = 0.5
        assertEquals(0.6875, small, 1e-9);
        assertEquals(0.5, large, 1e-9);
        assertTrue(small > large);
        // memory dimension follows the same rule: 0.5 * (1 + 0.5 * (1 - 8/32)) = 0.6875
        assertEquals(0.6875, view.getWorkerLoad(1).get().memUsedPct, 1e-9);
    }

    @Test
    public void testCapacityWeightZeroPreservesBehavior() {
        view.setReferenceCapacityForTest(16.0, 32L * 1024 * 1024 * 1024);
        view.putLoadForTest(1, 0.5, 0.5, true, 4.0, 8L * 1024 * 1024 * 1024);
        Optional<AutoDopResourceView.WorkerLoad> load = view.getWorkerLoad(1);
        // weight = 0 (default): output is exactly the usage, unchanged by capacity
        assertEquals(0.5, load.get().cpuUsed, 1e-9);
        assertEquals(0.5, load.get().memUsedPct, 1e-9);
    }

    @Test
    public void testCapacityMissingIsNeutral() {
        Config.auto_dop_capacity_weight = 0.5;
        view.setReferenceCapacityForTest(16.0, 32L * 1024 * 1024 * 1024);
        // no capacity snapshot for this worker: both dimensions stay at the raw usage
        view.putLoadForTest(6, 0.7, 0.7, true);
        assertEquals(0.7, view.getWorkerLoad(6).get().cpuUsed, 1e-9);
        assertEquals(0.7, view.getWorkerLoad(6).get().memUsedPct, 1e-9);
    }

    @Test
    public void testCapacityTensionClampedToOne() {
        Config.auto_dop_capacity_weight = 0.5;
        view.setReferenceCapacityForTest(16.0, 32L * 1024 * 1024 * 1024);
        view.putLoadForTest(7, 0.9, 0.9, true, 4.0, 8L * 1024 * 1024 * 1024);
        // 0.9 * (1 + 0.5 * 0.75) = 1.2375 -> clamped to 1.0
        assertEquals(1.0, view.getWorkerLoad(7).get().cpuUsed, 1e-9);
    }

    @Test
    public void testFillWorkerLoadsCarriesCapacityAdjustedValues() {
        Config.auto_dop_capacity_weight = 0.5;
        view.setReferenceCapacityForTest(16.0, 32L * 1024 * 1024 * 1024);
        view.putLoadForTest(10, 0.5, 0.5, true, 4.0, 8L * 1024 * 1024 * 1024);
        AutoDopRequest request = new AutoDopRequest();
        AutoDopResourceView.fillWorkerLoads(request, Arrays.asList(10L));
        assertEquals(0.6875, request.workerLoads.get("10").cpuUsed, 1e-9);
    }
}
