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

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.starrocks.common.Config;
import com.starrocks.common.StarRocksException;
import com.starrocks.common.util.DebugUtil;
import com.starrocks.qe.ConnectContext;
import com.starrocks.thrift.TQueryOptions;
import com.starrocks.thrift.TQueryType;
import com.starrocks.thrift.TUniqueId;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Collections;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Unit tests for the AutoDOP gate (bypass instead of throwing, DD01 §3.1-P4), the configurable
 * decision degradation ({@code auto_dop_fail_on_error}, G1) and the null-safe trace register.
 * The decision paths are exercised through the package-visible seams
 * ({@link AutoDopController#decide(AutoDopRequest)} and the query-options overload of
 * {@link AutoDopController#isSupported}) with a mock client, without a full JobSpec/ExecutionDAG.
 */
public class AutoDopControllerTest {
    private ConnectContext connectContext;
    private Path traceDir;

    @BeforeEach
    public void setUp() throws Exception {
        connectContext = new ConnectContext();
        Config.auto_dop_fail_on_error = false;
        traceDir = Files.createTempDirectory("autodop-controller-test");
        AutoDopTraceManager.clearForTest();
        AutoDopTraceManager.setTraceDirForTest(traceDir.toString());
    }

    @AfterEach
    public void tearDown() throws Exception {
        AutoDopController.resetClientForTest();
        AutoDopTraceManager.clearForTest();
        Config.auto_dop_fail_on_error = false;
        try (Stream<Path> stream = Files.list(traceDir)) {
            for (Path file : stream.collect(Collectors.toList())) {
                Files.deleteIfExists(file);
            }
        }
        Files.deleteIfExists(traceDir);
    }

    private static TQueryOptions selectOptions() {
        TQueryOptions options = new TQueryOptions();
        options.setQuery_type(TQueryType.SELECT);
        return options;
    }

    @Test
    public void testIsSupportedTrueForSelectPipelineQuery() {
        Assertions.assertTrue(AutoDopController.isSupported(connectContext, selectOptions(), true, 5));
    }

    @Test
    public void testIsSupportedBypassesFewerThanFiveFragments() {
        for (int fragmentCount = 0; fragmentCount < 5; fragmentCount++) {
            Assertions.assertFalse(AutoDopController.isSupported(
                    connectContext, selectOptions(), true, fragmentCount));
        }
        Assertions.assertTrue(AutoDopController.isSupported(connectContext, selectOptions(), true, 5));
        Assertions.assertTrue(AutoDopController.isSupported(connectContext, selectOptions(), true, 6));
    }

    @Test
    public void testIsSupportedBypassesNonSelect() {
        Assertions.assertFalse(AutoDopController.isSupported(connectContext, null, true, 5));

        TQueryOptions options = new TQueryOptions();
        options.setQuery_type(TQueryType.LOAD);
        Assertions.assertFalse(AutoDopController.isSupported(connectContext, options, true, 5));
    }

    @Test
    public void testIsSupportedBypassesPipelineDisabled() {
        Assertions.assertFalse(AutoDopController.isSupported(connectContext, selectOptions(), false, 5));
    }

    @Test
    public void testIsSupportedBypassesRuntimeAdaptiveDop() {
        connectContext.getSessionVariable().setEnableRuntimeAdaptiveDop(true);
        Assertions.assertFalse(AutoDopController.isSupported(connectContext, selectOptions(), true, 5));
    }

    @Test
    public void testIsSupportedBypassesMissingContext() {
        Assertions.assertFalse(AutoDopController.isSupported(null, null, true, 5));
    }

    @Test
    public void testDecideFallsBackOnIoErrorByDefault() throws Exception {
        AutoDopController.setClientForTest(request -> {
            throw new IOException("endpoint unreachable");
        });
        AutoDopResult result = AutoDopController.decide(new AutoDopRequest());
        Assertions.assertNotNull(result.getSkipReason());
        Assertions.assertNull(result.getDecidedDop());
    }

    @Test
    public void testDecideCanFailFastForDiagnostics() {
        Config.auto_dop_fail_on_error = true;
        AutoDopController.setClientForTest(request -> {
            throw new IOException("endpoint unreachable");
        });
        Assertions.assertThrows(StarRocksException.class,
                () -> AutoDopController.decide(new AutoDopRequest()));
    }

    @Test
    public void testDecideFallsBackOnInvalidResponseByDefault() throws Exception {
        AutoDopController.setClientForTest(request -> {
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = "some-other-query";
            response.decisions = Collections.singletonList(new AutoDopResponse.Decision("F00", 4, "test"));
            return response;
        });
        AutoDopResult result = AutoDopController.decide(new AutoDopRequest());
        Assertions.assertNotNull(result);
        Assertions.assertNotNull(result.getSkipReason());
        Assertions.assertTrue(result.getSkipReason().startsWith("invalid decision response"));
    }

    @Test
    public void testDecideFailsOnInvalidResponseWhenConfigured() {
        Config.auto_dop_fail_on_error = true;
        AutoDopController.setClientForTest(request -> {
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = "some-other-query";
            response.decisions = Collections.singletonList(new AutoDopResponse.Decision("F00", 4, "test"));
            return response;
        });
        Assertions.assertThrows(StarRocksException.class,
                () -> AutoDopController.decide(new AutoDopRequest()));
    }

    @Test
    public void testDecideFallsBackOnLowConfidence() throws Exception {
        AutoDopController.setClientForTest(request -> {
            AutoDopResponse response = new AutoDopResponse();
            response.queryId = request.queryId;
            response.confidence = Config.auto_dop_min_confidence / 2.0;
            return response;
        });
        AutoDopResult result = AutoDopController.decide(new AutoDopRequest());
        Assertions.assertNotNull(result.getSkipReason());
        Assertions.assertTrue(result.getSkipReason().contains("confidence"));
    }

    @Test
    public void testRegisterIsNullSafe() {
        // must not throw and must not create a trace record
        AutoDopTraceManager.register(null);
        Assertions.assertNull(AutoDopTraceManager.traceFragmentsForTest(new TUniqueId(0x41, 0x1)));
    }

    @Test
    public void testRegisterPersistsSkipReasonWithoutRequestPayload() throws Exception {
        AutoDopRequest request = new AutoDopRequest();
        request.queryId = DebugUtil.printId(new TUniqueId(0x42, 0x2));
        AutoDopResult degraded = new AutoDopResult(request, null, null, null, null, 0L,
                "decision request failed: endpoint unreachable");
        AutoDopTraceManager.register(degraded);

        connectContext.setExecutionId(new TUniqueId(0x42, 0x2));
        AutoDopTraceManager.finishAndPersist(connectContext, null, null);
        AutoDopTraceManager.flushForTest();

        List<Path> files;
        try (Stream<Path> stream = Files.list(traceDir)) {
            files = stream.collect(Collectors.toList());
        }
        Assertions.assertEquals(1, files.size());
        String line = new String(Files.readAllBytes(files.get(0)), StandardCharsets.UTF_8).trim();
        JsonObject json = JsonParser.parseString(line).getAsJsonObject();
        Assertions.assertEquals("decision request failed: endpoint unreachable",
                json.get("skip_reason").getAsString());
        Assertions.assertFalse(json.has("request"), "degraded trace must not persist the request payload");
    }
}
