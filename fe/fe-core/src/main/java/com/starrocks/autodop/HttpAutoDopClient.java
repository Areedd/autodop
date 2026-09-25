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

import com.starrocks.persist.gson.GsonUtils;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public class HttpAutoDopClient implements AutoDopClient {
    private final String endpoint;
    private final int timeoutMs;

    /**
     * @param endpoint  decision service base URL
     * @param timeoutMs per-phase timeout in milliseconds: it bounds the TCP connect phase and,
     *                  separately, each read phase of the request. The total wall time of
     *                  {@link #decide} can therefore reach up to {@code 2 * timeoutMs} (or more
     *                  when the server streams a large response body).
     *                  <p>Note the measurement difference: this timeout covers the FE-side
     *                  network round trip (connect + read). The decision service reports its own
     *                  {@code decision_latency_ms} (AutoDopResult#decisionLatencyMs, sampled
     *                  around {@code client().decide()}), which measures server-side processing
     *                  only and excludes the network transport. When comparing the two, the FE
     *                  wall time is expected to be larger by roughly the RTT (and by up to a full
     *                  extra timeout on failure).
     */
    public HttpAutoDopClient(String endpoint, int timeoutMs) {
        this.endpoint = endpoint == null ? "" : endpoint;
        this.timeoutMs = Math.max(1, timeoutMs);
    }

    @Override
    public AutoDopResponse decide(AutoDopRequest request) throws IOException {
        String url = endpoint.endsWith("/") ? endpoint + "v1/decide" : endpoint + "/v1/decide";
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(timeoutMs);
        connection.setReadTimeout(timeoutMs);
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        byte[] payload = GsonUtils.GSON.toJson(request).getBytes(StandardCharsets.UTF_8);
        connection.setFixedLengthStreamingMode(payload.length);
        try (OutputStream outputStream = connection.getOutputStream()) {
            outputStream.write(payload);
        }

        int statusCode = connection.getResponseCode();
        InputStream stream = statusCode >= 200 && statusCode < 300 ?
                connection.getInputStream() : connection.getErrorStream();
        String body = "";
        if (stream != null) {
            try (InputStream inputStream = stream) {
                body = new String(inputStream.readAllBytes(), StandardCharsets.UTF_8);
            }
        }
        if (statusCode < 200 || statusCode >= 300) {
            throw new IOException("AutoDOP backend returned HTTP " + statusCode + ": " + body);
        }
        return GsonUtils.GSON.fromJson(body, AutoDopResponse.class);
    }
}
