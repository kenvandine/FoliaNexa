package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

class DisplayJsonTest {

    @Test
    void parsesMotdAndIcon() {
        String body = "{\"motd\": \"<red>Sullivan SMP</red>\", \"icon_base64\": \"aGVsbG8=\"}";
        ProxyDisplay display = DisplayJson.parse(body);
        assertEquals("<red>Sullivan SMP</red>", display.motd());
        assertEquals("aGVsbG8=", display.iconBase64());
    }

    @Test
    void nullIconParsesToNull() {
        String body = "{\"motd\": \"A Velocity Server\", \"icon_base64\": null}";
        assertNull(DisplayJson.parse(body).iconBase64());
    }

    @Test
    void missingIconFieldParsesToNull() {
        String body = "{\"motd\": \"A Velocity Server\"}";
        assertNull(DisplayJson.parse(body).iconBase64());
    }

    @Test
    void motdContainingQuotesAndUnicodeSurvivesRoundTrip() {
        String body = "{\"motd\": \"<gray>\\\"quoted\\\"</gray> \\u2764\", \"icon_base64\": null}";
        assertEquals("<gray>\"quoted\"</gray> \u2764", DisplayJson.parse(body).motd());
    }
}
