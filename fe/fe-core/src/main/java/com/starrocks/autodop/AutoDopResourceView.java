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
import com.starrocks.common.ThreadPoolManager;
import com.starrocks.server.GlobalStateMgr;
import com.starrocks.system.ComputeNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Per-node resource view shared by the load correction (DD03 §4.2) and the instance-level DOP
 * adjustment (DD04 §2.3) paths.
 *
 * <p>The raw data already exists: each BE reports {@code TResourceUsage} (cpu_used_permille,
 * mem_used_bytes, mem_limit_bytes) to the FE periodically, and {@link ComputeNode} stores the
 * latest values. This view samples those values on a fixed interval, smooths them with an EWMA
 * filter to remove transient jitter, and exposes a unified per-worker lookup.
 *
 * <p>Workers without a fresh sample are treated as neutral (load 0, not fresh): both consumers
 * then keep the baseline DOP instead of adjusting.
 */
public final class AutoDopResourceView {
    private static final Logger LOG = LogManager.getLogger(AutoDopResourceView.class);
    private static volatile AutoDopResourceView instance;
    private static volatile AutoDopResourceView instanceForTest;

    private final Map<Long, SmoothedLoad> loads = new ConcurrentHashMap<>();
    private final ScheduledExecutorService sampler;
    private volatile boolean started;

    AutoDopResourceView() {
        sampler = ThreadPoolManager.newDaemonScheduledThreadPool(
                1, "auto-dop-resource-view", true);
    }

    public static AutoDopResourceView getInstance() {
        if (instance == null) {
            synchronized (AutoDopResourceView.class) {
                if (instance == null) {
                    instance = new AutoDopResourceView();
                }
            }
        }
        instance.ensureStarted();
        return instance;
    }

    public void ensureStarted() {
        if (started) {
            return;
        }
        synchronized (this) {
            if (started) {
                return;
            }
            sampler.scheduleWithFixedDelay(this::sampleOnce, 0,
                    Config.auto_dop_resource_view_interval_ms, TimeUnit.MILLISECONDS);
            started = true;
        }
    }

    public void shutdown() {
        started = false;
        sampler.shutdownNow();
    }

    /**
     * Snapshot of the smoothed load of one worker. {@code fresh} is false when the BE reported
     * no resource usage within the freshness window, or when the worker is not alive.
     */
    public static class WorkerLoad {
        public final double cpuUsed;
        public final double memUsedPct;
        public final boolean fresh;

        public WorkerLoad(double cpuUsed, double memUsedPct, boolean fresh) {
            this.cpuUsed = cpuUsed;
            this.memUsedPct = memUsedPct;
            this.fresh = fresh;
        }
    }

    static class SmoothedLoad {
        double cpuUsed;
        double memUsedPct;
        boolean fresh;

        SmoothedLoad(double cpuUsed, double memUsedPct, boolean fresh) {
            this.cpuUsed = cpuUsed;
            this.memUsedPct = memUsedPct;
            this.fresh = fresh;
        }
    }

    /**
     * Returns the smoothed load of a worker, or empty when the worker is unknown.
     * Consumers treat an empty result as neutral (keep the baseline DOP).
     */
    public Optional<WorkerLoad> getWorkerLoad(long workerId) {
        SmoothedLoad load = loads.get(workerId);
        if (load == null) {
            return Optional.empty();
        }
        return Optional.of(new WorkerLoad(load.cpuUsed, load.memUsedPct, load.fresh));
    }

    /**
     * Test hook: creates an unstarted view whose load map can be pre-populated with
     * {@link #putLoadForTest} and installed via {@link #setInstanceForTest}.
     */
    public static AutoDopResourceView newInstanceForTest() {
        return new AutoDopResourceView();
    }

    /**
     * Test hook: replaces the singleton with a view whose load map is pre-populated.
     */
    public static void setInstanceForTest(AutoDopResourceView view) {
        instanceForTest = view;
    }

    public static void resetInstanceForTest() {
        instanceForTest = null;
    }

    /**
     * Test hook: populates a synthetic load for a worker without starting the sampler.
     */
    public void putLoadForTest(long workerId, double cpuUsed, double memUsedPct, boolean fresh) {
        loads.put(workerId, new SmoothedLoad(cpuUsed, memUsedPct, fresh));
    }

    private void sampleOnce() {
        try {
            List<ComputeNode> nodes = GlobalStateMgr.getCurrentState().getNodeMgr()
                    .getClusterInfo().backendAndComputeNodeStream()
                    .collect(java.util.stream.Collectors.toList());
            for (ComputeNode node : nodes) {
                long workerId = node.getId();
                boolean fresh = node.isResourceUsageFresh();
                double cpuUsed = fresh ? node.getCpuUsedPermille() / 1000.0 : 0.0;
                double memUsedPct = fresh ? node.getMemUsedPct() : 0.0;
                applyRawSample(workerId, cpuUsed, memUsedPct, fresh);
            }
        } catch (Throwable e) {
            LOG.warn("failed to sample AutoDOP resource view", e);
        }
    }

    /**
     * Applies one raw sample to the smoothed state of a worker. The first sample is taken as-is;
     * later samples are merged with the EWMA filter ({@code alpha = auto_dop_load_ewma_alpha}).
     * Package-visible for unit tests.
     */
    void applyRawSample(long workerId, double rawCpuUsed, double rawMemUsedPct, boolean fresh) {
        SmoothedLoad current = loads.get(workerId);
        if (current == null) {
            loads.put(workerId, new SmoothedLoad(rawCpuUsed, rawMemUsedPct, fresh));
            return;
        }
        double alpha = Config.auto_dop_load_ewma_alpha;
        current.cpuUsed = alpha * rawCpuUsed + (1 - alpha) * current.cpuUsed;
        current.memUsedPct = alpha * rawMemUsedPct + (1 - alpha) * current.memUsedPct;
        current.fresh = fresh;
    }

    Map<Long, SmoothedLoad> loadsForTest() {
        return loads;
    }

    public static AutoDopResourceView effectiveInstance() {
        return instanceForTest != null ? instanceForTest : getInstance();
    }

    /**
     * Fills per-worker loads for the given worker ids into the request, using the effective
     * (possibly test-injected) resource view.
     */
    static void fillWorkerLoads(AutoDopRequest request, Iterable<Long> workerIds) {
        AutoDopResourceView view = effectiveInstance();
        for (long workerId : workerIds) {
            Optional<WorkerLoad> load = view.getWorkerLoad(workerId);
            if (!load.isPresent()) {
                continue;
            }
            WorkerLoad value = load.get();
            request.workerLoads.put(String.valueOf(workerId),
                    new AutoDopRequest.WorkerLoad(value.cpuUsed, value.memUsedPct, value.fresh));
        }
    }
}
