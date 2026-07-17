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

import com.google.gson.annotations.SerializedName;

import java.util.ArrayList;
import java.util.List;

public class AutoDopResponse {
    @SerializedName("version")
    public int version = 1;

    @SerializedName("query_id")
    public String queryId = "";

    @SerializedName("decisions")
    public List<Decision> decisions = new ArrayList<>();

    public static class Decision {
        @SerializedName("fragment_id")
        public String fragmentId = "";

        @SerializedName("dop")
        public int dop;

        @SerializedName("reason")
        public String reason = "";

        public Decision() {
        }

        public Decision(String fragmentId, int dop, String reason) {
            this.fragmentId = fragmentId;
            this.dop = dop;
            this.reason = reason;
        }
    }
}
