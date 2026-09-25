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

import java.util.Map;

public class AutoDopResult {
    private final AutoDopRequest request;
    private final AutoDopResponse response;
    private final Map<String, Integer> originalDop;
    private final Map<String, Integer> decidedDop;
    private final Map<String, Integer> installedDop;
    private final long decisionLatencyMs;
    // null = normal decision; non-null = degraded (AutoDOP skipped, query keeps the original DOP).
    private final String skipReason;

    public AutoDopResult(AutoDopRequest request, AutoDopResponse response,
                         Map<String, Integer> originalDop, Map<String, Integer> decidedDop,
                         Map<String, Integer> installedDop, long decisionLatencyMs) {
        this(request, response, originalDop, decidedDop, installedDop, decisionLatencyMs, null);
    }

    public AutoDopResult(AutoDopRequest request, AutoDopResponse response,
                         Map<String, Integer> originalDop, Map<String, Integer> decidedDop,
                         Map<String, Integer> installedDop, long decisionLatencyMs, String skipReason) {
        this.request = request;
        this.response = response;
        this.originalDop = originalDop;
        this.decidedDop = decidedDop;
        this.installedDop = installedDop;
        this.decisionLatencyMs = decisionLatencyMs;
        this.skipReason = skipReason;
    }

    public AutoDopRequest getRequest() {
        return request;
    }

    public AutoDopResponse getResponse() {
        return response;
    }

    public Map<String, Integer> getOriginalDop() {
        return originalDop;
    }

    public Map<String, Integer> getDecidedDop() {
        return decidedDop;
    }

    public Map<String, Integer> getInstalledDop() {
        return installedDop;
    }

    public long getDecisionLatencyMs() {
        return decisionLatencyMs;
    }

    public String getSkipReason() {
        return skipReason;
    }
}
