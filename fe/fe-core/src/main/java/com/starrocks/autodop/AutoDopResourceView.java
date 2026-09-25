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
import com.starrocks.system.BackendResourceStat;
import com.starrocks.system.ComputeNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Per-node resource view for fragment-level load correction.
 *
 * <p>The raw data already exists: each BE reports {@code TResourceUsage} (cpu_used_permille,
 * mem_used_bytes, mem_limit_bytes) to the FE periodically, and {@link ComputeNode} stores the
 * latest values. This view samples those values on a fixed interval, smooths them with an EWMA
 * filter to remove transient jitter, and exposes a unified per-worker lookup.
 *
 * <p>Workers without a fresh sample are treated as neutral (load = {@code auto_dop_resource_neutral_cpu/_mem},
 * not fresh): load correction keeps the policy DOP unchanged. The freshness
 * window is {@link ComputeNode#isResourceUsageFresh()} by default, or a dedicated window
 * ({@code auto_dop_resource_fresh_interval_ms} &gt; 0) tracked per worker.
 *
 * <p>When {@code auto_dop_capacity_weight} &gt; 0, the smoothed usage is additionally scaled by the
 * worker capacity relative to the cluster reference (rolling P99 of the sampled capacities):
 * {@code tension = min(1, usage * (1 + weight * (1 - normalized_capacity)))}. With the default
 * weight 0 the output is exactly the usage (historical behavior). Missing capacity data (e.g. an
 * old BE that never reported cores, or a pure CN) is neutral: that dimension is not scaled.
 */
public final class AutoDopResourceView {
    private static final Logger LOG = LogManager.getLogger(AutoDopResourceView.class);
    private static volatile AutoDopResourceView instance;
    private static volatile AutoDopResourceView instanceForTest;

    private final Map<Long, SmoothedLoad> loads = new ConcurrentHashMap<>();
    private final ScheduledExecutorService sampler;
    private volatile boolean started;

    // Rolling cluster reference capacities (P99 of the last sample cycle), used only when
    // auto_dop_capacity_weight > 0. -1 = no capacity data seen yet.
    private volatile double referenceCpuCapacity = -1.0;
    private volatile double referenceMemCapacity = -1.0;

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
        // Timestamp of the last sample where the node reported fresh resource usage; used only
        // when auto_dop_resource_fresh_interval_ms > 0. 0 = never seen fresh.
        long lastSeenFreshMs;
        // Capacity snapshot of the worker at sample time (cores / mem limit bytes).
        // -1 = unknown (capacity normalization is neutral for that dimension).
        double cpuCapacity;
        double memCapacity;

        SmoothedLoad(double cpuUsed, double memUsedPct, boolean fresh, long sampleMs,
                     double cpuCapacity, double memCapacity) {
            this.cpuUsed = cpuUsed;
            this.memUsedPct = memUsedPct;
            this.cpuCapacity = cpuCapacity;
            this.memCapacity = memCapacity;
            if (Config.auto_dop_resource_fresh_interval_ms > 0) {
                this.lastSeenFreshMs = fresh ? sampleMs : 0L;
                this.fresh = this.lastSeenFreshMs > 0
                        && (sampleMs - this.lastSeenFreshMs) <= Config.auto_dop_resource_fresh_interval_ms;
            } else {
                this.lastSeenFreshMs = 0L;
                this.fresh = fresh;
            }
        }
    }

    /**
     * Returns the smoothed load of a worker, or empty when the worker is unknown.
     * Consumers treat an empty result as neutral (keep the baseline DOP).
     *
     * <p>When the dedicated freshness window ({@code auto_dop_resource_fresh_interval_ms} &gt; 0) is
     * enabled, freshness is evaluated lazily here from the per-worker {@code lastSeenFreshMs}.
     * When {@code auto_dop_capacity_weight} &gt; 0, the smoothed usage is scaled by the worker
     * capacity relative to the cluster reference (both dimensions, clamped to 1.0).
     */
    public Optional<WorkerLoad> getWorkerLoad(long workerId) {
        SmoothedLoad load = loads.get(workerId);
        if (load == null) {
            return Optional.empty();
        }
        boolean fresh = load.fresh;
        if (Config.auto_dop_resource_fresh_interval_ms > 0) {
            fresh = load.lastSeenFreshMs > 0
                    && (System.currentTimeMillis() - load.lastSeenFreshMs)
                    <= Config.auto_dop_resource_fresh_interval_ms;
        }
        double cpuUsed = load.cpuUsed;
        double memUsedPct = load.memUsedPct;
        if (Config.auto_dop_capacity_weight > 0.0) {
            cpuUsed = capacityTension(cpuUsed, load.cpuCapacity, referenceCpuCapacity);
            memUsedPct = capacityTension(memUsedPct, load.memCapacity, referenceMemCapacity);
        }
        return Optional.of(new WorkerLoad(cpuUsed, memUsedPct, fresh));
    }

    /**
     * Capacity-normalized tension (DD04 §3.2): with the same usage, a smaller node (capacity
     * below the cluster reference) gets a higher tension, clamped to 1.0. Missing capacity or
     * reference data is neutral (output = usage).
     */
    private static double capacityTension(double usage, double capacity, double reference) {
        if (capacity <= 0.0 || reference <= 0.0) {
            return usage;
        }
        double normalized = Math.min(1.0, capacity / reference);
        return Math.min(1.0, usage * (1.0 + Config.auto_dop_capacity_weight * (1.0 - normalized)));
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
        putLoadForTest(workerId, cpuUsed, memUsedPct, fresh, -1.0, -1.0);
    }

    /**
     * Test hook: populates a synthetic load together with a capacity snapshot (cores / mem limit
     * bytes; -1 = unknown) so the capacity normalization can be exercised without a sampler.
     */
    public void putLoadForTest(long workerId, double cpuUsed, double memUsedPct, boolean fresh,
                               double cpuCapacity, double memCapacity) {
        loads.put(workerId, new SmoothedLoad(cpuUsed, memUsedPct, fresh, System.currentTimeMillis(),
                cpuCapacity, memCapacity));
    }

    /**
     * Test hook: overrides the rolling cluster reference capacities.
     */
    public void setReferenceCapacityForTest(double cpuCapacity, double memCapacity) {
        referenceCpuCapacity = cpuCapacity;
        referenceMemCapacity = memCapacity;
    }

    private void sampleOnce() {
        try {
            List<ComputeNode> nodes = GlobalStateMgr.getCurrentState().getNodeMgr()
                    .getClusterInfo().backendAndComputeNodeStream()
                    .collect(java.util.stream.Collectors.toList());
            Map<Long, Integer> coresPerBe = BackendResourceStat.getInstance().getNumHardwareCoresPerBe();
            long sampleMs = System.currentTimeMillis();
            double[] cpuCapacities = new double[nodes.size()];
            int cpuCapacityCount = 0;
            double[] memCapacities = new double[nodes.size()];
            int memCapacityCount = 0;
            for (ComputeNode node : nodes) {
                long workerId = node.getId();
                boolean nodeFresh = node.isResourceUsageFresh();
                double cpuUsed = nodeFresh ? node.getCpuUsedPermille() / 1000.0
                        : Config.auto_dop_resource_neutral_cpu;
                double memUsedPct = nodeFresh ? node.getMemUsedPct()
                        : Config.auto_dop_resource_neutral_mem;
                Integer cores = coresPerBe.get(workerId);
                double cpuCapacity = cores == null ? -1.0 : cores;
                double memCapacity = node.getMemLimitBytes();
                applyRawSample(workerId, cpuUsed, memUsedPct, nodeFresh, sampleMs,
                        cpuCapacity, memCapacity);
                if (cpuCapacity > 0) {
                    cpuCapacities[cpuCapacityCount++] = cpuCapacity;
                }
                if (memCapacity > 0) {
                    memCapacities[memCapacityCount++] = memCapacity;
                }
            }
            referenceCpuCapacity = percentile(cpuCapacities, cpuCapacityCount);
            referenceMemCapacity = percentile(memCapacities, memCapacityCount);
        } catch (Throwable e) {
            LOG.warn("failed to sample AutoDOP resource view", e);
        }
    }

    private static double percentile(double[] values, int count) {
        if (count == 0) {
            return -1.0;
        }
        Arrays.sort(values, 0, count);
        int index = Math.min(count - 1, (int) Math.ceil(0.99 * count) - 1);
        return values[index];
    }

    /**
     * Applies one raw sample to the smoothed state of a worker. The first sample is taken as-is;
     * later samples are merged with the EWMA filter ({@code alpha = auto_dop_load_ewma_alpha}).
     * Package-visible for unit tests.
     */
    void applyRawSample(long workerId, double rawCpuUsed, double rawMemUsedPct, boolean fresh) {
        applyRawSample(workerId, rawCpuUsed, rawMemUsedPct, fresh, System.currentTimeMillis(), -1.0, -1.0);
    }

    /**
     * Full form with the sample timestamp and the worker capacity snapshot (used by the sampler;
     * package-visible for unit tests).
     */
    void applyRawSample(long workerId, double rawCpuUsed, double rawMemUsedPct, boolean fresh,
                        long sampleMs, double cpuCapacity, double memCapacity) {
        SmoothedLoad current = loads.get(workerId);
        if (current == null) {
            loads.put(workerId, new SmoothedLoad(rawCpuUsed, rawMemUsedPct, fresh, sampleMs,
                    cpuCapacity, memCapacity));
            return;
        }
        double alpha = Config.auto_dop_load_ewma_alpha;
        current.cpuUsed = alpha * rawCpuUsed + (1 - alpha) * current.cpuUsed;
        current.memUsedPct = alpha * rawMemUsedPct + (1 - alpha) * current.memUsedPct;
        if (Config.auto_dop_resource_fresh_interval_ms > 0) {
            if (fresh) {
                current.lastSeenFreshMs = sampleMs;
            }
            current.fresh = current.lastSeenFreshMs > 0
                    && (sampleMs - current.lastSeenFreshMs) <= Config.auto_dop_resource_fresh_interval_ms;
        } else {
            current.fresh = fresh;
        }
        if (cpuCapacity > 0) {
            current.cpuCapacity = cpuCapacity;
        }
        if (memCapacity > 0) {
            current.memCapacity = memCapacity;
        }
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
