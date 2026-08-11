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
    }

    @AfterEach
    public void tearDown() {
        view.shutdown();
        AutoDopResourceView.resetInstanceForTest();
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
}
