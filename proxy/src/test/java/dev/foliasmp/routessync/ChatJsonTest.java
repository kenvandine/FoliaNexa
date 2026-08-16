package dev.foliasmp.routessync;

import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ChatJsonTest {

    @Test
    void parsesMultiplePendingMessages() {
        String body = "["
                + "{\"world\": \"world-lobby\", \"author\": \"someone\", \"message\": \"hey\"},"
                + "{\"world\": null, \"author\": \"other\", \"message\": \"hi all\"}"
                + "]";
        List<PendingChatMessage> messages = ChatJson.parsePending(body);

        assertEquals(2, messages.size());
        assertEquals(new PendingChatMessage("world-lobby", "someone", "hey"), messages.get(0));
        assertNull(messages.get(1).world());
        assertEquals("hi all", messages.get(1).message());
    }

    @Test
    void emptyPendingListParsesToEmpty() {
        assertTrue(ChatJson.parsePending("[]").isEmpty());
    }

    @Test
    void pendingMessageContainingQuotesAndUnicodeSurvivesRoundTrip() {
        String body = "[{\"world\": null, \"author\": \"a\\\"b\", \"message\": \"quoted \\\"text\\\" \\u2764\"}]";
        PendingChatMessage msg = ChatJson.parsePending(body).get(0);
        assertEquals("a\"b", msg.author());
        assertEquals("quoted \"text\" \u2764", msg.message());
    }

    @Test
    void buildReportBodyProducesValidJsonWithGivenFields() {
        String body = ChatJson.buildReportBody("world-overworld", "Steve", "hello, \"world\"!");
        var obj = JsonParser.parseString(body).getAsJsonObject();
        assertEquals("world-overworld", obj.get("world").getAsString());
        assertEquals("Steve", obj.get("player").getAsString());
        assertEquals("hello, \"world\"!", obj.get("message").getAsString());
    }
}
