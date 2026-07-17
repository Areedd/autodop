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

import com.starrocks.common.StarRocksException;
import org.apache.commons.lang3.StringUtils;

import java.util.LinkedHashMap;
import java.util.Map;

public final class AutoDopDecisionValidator {
    private AutoDopDecisionValidator() {
    }

    public static Map<String, Integer> validateOrThrow(AutoDopRequest request, AutoDopResponse response)
            throws StarRocksException {
        if (request == null || response == null) {
            throw new StarRocksException("AutoDOP request and response must be present");
        }
        if (request.version != response.version) {
            throw new StarRocksException("AutoDOP protocol version mismatch");
        }
        if (!StringUtils.equals(request.queryId, response.queryId)) {
            throw new StarRocksException("AutoDOP query id mismatch");
        }
        if (request.fragments == null || response.decisions == null
                || request.fragments.size() != response.decisions.size()) {
            throw new StarRocksException("AutoDOP must return one decision per fragment");
        }

        Map<String, AutoDopRequest.FragmentInfo> fragments = new LinkedHashMap<>();
        for (AutoDopRequest.FragmentInfo fragment : request.fragments) {
            if (fragment == null || StringUtils.isBlank(fragment.fragmentId)
                    || fragments.put(fragment.fragmentId, fragment) != null) {
                throw new StarRocksException("AutoDOP request contains an invalid fragment");
            }
        }

        Map<String, Integer> decisions = new LinkedHashMap<>();
        for (AutoDopResponse.Decision decision : response.decisions) {
            if (decision == null || StringUtils.isBlank(decision.fragmentId)) {
                throw new StarRocksException("AutoDOP response contains an invalid decision");
            }
            AutoDopRequest.FragmentInfo fragment = fragments.get(decision.fragmentId);
            if (fragment == null) {
                throw new StarRocksException("AutoDOP response contains unknown fragment " + decision.fragmentId);
            }
            if (decisions.put(decision.fragmentId, decision.dop) != null) {
                throw new StarRocksException("AutoDOP response repeats fragment " + decision.fragmentId);
            }
            if (fragment.candidateDops == null || !fragment.candidateDops.contains(decision.dop)) {
                throw new StarRocksException(
                        "AutoDOP response contains invalid DOP for fragment " + decision.fragmentId);
            }
        }
        return decisions;
    }
}
