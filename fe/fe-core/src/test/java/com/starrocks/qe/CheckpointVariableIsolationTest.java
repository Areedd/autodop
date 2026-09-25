// Copyright 2021-present StarRocks, Inc. All rights reserved.
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//     https://www.apache.org/licenses/LICENSE-2.0
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package com.starrocks.qe;

import com.google.gson.stream.JsonReader;
import com.starrocks.analysis.VariableExpr;
import com.starrocks.common.DdlException;
import com.starrocks.persist.GlobalVarPersistInfo;
import com.starrocks.persist.ImageWriter;
import com.starrocks.persist.metablock.SRMetaBlockReaderV2;
import com.starrocks.sql.ast.SetType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

public class CheckpointVariableIsolationTest {
    private boolean originalEnabled;
    private int originalLimit;
    private double originalMemoryLimit;

    @BeforeEach
    public void rememberGlobals() {
        originalEnabled = GlobalVariable.isEnableQueryQueueSelect();
        originalLimit = GlobalVariable.getQueryQueueConcurrencyLimit();
        originalMemoryLimit = GlobalVariable.getQueryQueueMemUsedPctLimit();
    }

    @AfterEach
    public void restoreGlobals() {
        GlobalVariable.setEnableQueryQueueSelect(originalEnabled);
        GlobalVariable.setQueryQueueConcurrencyLimit(originalLimit);
        GlobalVariable.setQueryQueueMemUsedPctLimit(originalMemoryLimit);
    }

    private static byte[] save(VariableMgr manager) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ImageWriter writer = new ImageWriter("", 0);
        writer.setOutputStream(output);
        manager.save(writer);
        return output.toByteArray();
    }

    private static void load(VariableMgr manager, byte[] image) throws Exception {
        JsonReader json = new JsonReader(new InputStreamReader(new ByteArrayInputStream(image), StandardCharsets.UTF_8));
        json.setLenient(true);
        SRMetaBlockReaderV2 reader = new SRMetaBlockReaderV2(json);
        try {
            manager.load(reader);
        } finally {
            reader.close();
        }
    }

    private static void replay(VariableMgr manager, String json) throws DdlException {
        GlobalVarPersistInfo info = new GlobalVarPersistInfo();
        info.setPersistJsonString(json);
        manager.replayGlobalVariableV2(info);
    }

    @Test
    public void checkpointLoadReplayAndSavePreserveBothStates() throws Exception {
        GlobalVariable.setEnableQueryQueueSelect(false);
        GlobalVariable.setQueryQueueConcurrencyLimit(0);
        GlobalVariable.setQueryQueueMemUsedPctLimit(0);
        VariableMgr serving = new VariableMgr(false);
        byte[] historical = save(serving);
        replay(serving, "{\"enable_query_queue_select\":true,\"query_queue_concurrency_limit\":8,"
                + "\"query_queue_mem_used_pct_limit\":60,\"query_timeout\":45}");

        VariableMgr checkpoint = new VariableMgr(true);
        load(checkpoint, historical);
        Assertions.assertTrue(GlobalVariable.isEnableQueryQueueSelect());
        Assertions.assertEquals(8, GlobalVariable.getQueryQueueConcurrencyLimit());
        Assertions.assertEquals(60, GlobalVariable.getQueryQueueMemUsedPctLimit());
        Assertions.assertEquals(45, serving.getDefaultSessionVariable().getQueryTimeoutS());
        Assertions.assertEquals(300, checkpoint.getDefaultSessionVariable().getQueryTimeoutS());
        replay(checkpoint, "{\"query_queue_concurrency_limit\":4,\"query_queue_mem_used_pct_limit\":25,"
                + "\"query_timeout\":90}");
        Assertions.assertEquals(8, GlobalVariable.getQueryQueueConcurrencyLimit());
        Assertions.assertEquals(60, GlobalVariable.getQueryQueueMemUsedPctLimit());
        Assertions.assertEquals("4", checkpoint.getValue(null,
                new VariableExpr("query_queue_concurrency_limit", SetType.GLOBAL)));

        byte[] result = save(checkpoint);
        VariableMgr recovered = new VariableMgr(false);
        load(recovered, result);
        Assertions.assertFalse(GlobalVariable.isEnableQueryQueueSelect());
        Assertions.assertEquals(4, GlobalVariable.getQueryQueueConcurrencyLimit());
        Assertions.assertEquals(25, GlobalVariable.getQueryQueueMemUsedPctLimit());
        Assertions.assertEquals(90, recovered.getDefaultSessionVariable().getQueryTimeoutS());
    }

    @Test
    public void servingReplayAndValidationRemainEffective() throws Exception {
        VariableMgr serving = new VariableMgr(false);
        replay(serving, "{\"enable_query_queue_select\":\"ON\",\"query_queue_concurrency_limit\":16}");
        Assertions.assertTrue(GlobalVariable.isEnableQueryQueueSelect());
        Assertions.assertEquals(16, GlobalVariable.getQueryQueueConcurrencyLimit());
        VariableMgr checkpoint = new VariableMgr(true);
        replay(checkpoint, "{\"enable_query_queue_select\":\"OFF\"}");
        Assertions.assertTrue(GlobalVariable.isEnableQueryQueueSelect());
        Assertions.assertEquals("false", checkpoint.getValue(null,
                new VariableExpr("enable_query_queue_select", SetType.GLOBAL)));
        Assertions.assertThrows(DdlException.class,
                () -> replay(checkpoint, "{\"enable_query_queue_select\":\"invalid\"}"));
        Assertions.assertEquals("false", checkpoint.getValue(null,
                new VariableExpr("enable_query_queue_select", SetType.GLOBAL)));
        replay(serving, "{\"enable_query_queue_select\":false}");
        Assertions.assertFalse(GlobalVariable.isEnableQueryQueueSelect());
    }

    @Test
    public void checkpointManagersKeepIndependentHistoricalValues() throws Exception {
        VariableMgr first = new VariableMgr(true);
        VariableMgr second = new VariableMgr(true);
        replay(first, "{\"query_queue_concurrency_limit\":4}");
        replay(second, "{\"query_queue_concurrency_limit\":16}");
        GlobalVariable.setQueryQueueConcurrencyLimit(32);
        byte[] firstImage = save(first);
        byte[] secondImage = save(second);
        load(new VariableMgr(false), firstImage);
        Assertions.assertEquals(4, GlobalVariable.getQueryQueueConcurrencyLimit());
        load(new VariableMgr(false), secondImage);
        Assertions.assertEquals(16, GlobalVariable.getQueryQueueConcurrencyLimit());
    }
}
