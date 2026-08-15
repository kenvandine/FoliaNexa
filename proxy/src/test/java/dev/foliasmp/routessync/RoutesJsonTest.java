package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RoutesJsonTest {

    @Test
    void parsesMultipleRoutesMatchingMgmtResponseShape() {
        String body = """
                {
                  "routes": [
                    {"world": "world-overworld", "type": "overworld", "address": "10.0.1.21:25565", "default": true},
                    {"world": "world-nether",    "type": "nether",    "address": "10.0.1.22:25565", "default": false},
                    {"world": "world-lobby",     "type": "lobby",     "address": "10.0.2.10:25565"}
                  ]
                }
                """;

        List<Route> routes = RoutesJson.parse(body);

        assertEquals(3, routes.size());

        Route overworld = routes.stream().filter(r -> r.world().equals("world-overworld")).findFirst().orElseThrow();
        assertEquals("overworld", overworld.type());
        assertEquals("10.0.1.21", overworld.host());
        assertEquals(25565, overworld.port());
        assertTrue(overworld.isDefault());

        Route nether = routes.stream().filter(r -> r.world().equals("world-nether")).findFirst().orElseThrow();
        assertFalse(nether.isDefault());
    }

    @Test
    void defaultsFieldMissingMeansNotDefault() {
        String body = "{\"routes\": [{\"world\": \"world-lobby\", \"type\": \"lobby\", \"address\": \"10.0.2.10:25565\"}]}";
        List<Route> routes = RoutesJson.parse(body);
        assertFalse(routes.get(0).isDefault());
    }

    @Test
    void emptyRoutesListParsesToEmpty() {
        assertTrue(RoutesJson.parse("{\"routes\": []}").isEmpty());
    }

    @Test
    void malformedAddressIsSkippedNotThrown() {
        String body = "{\"routes\": [{\"world\": \"broken\", \"type\": \"lobby\", \"address\": \"not-a-host-port\"}]}";
        assertTrue(RoutesJson.parse(body).isEmpty());
    }

    @Test
    void objectsMissingRequiredFieldsAreSkipped() {
        String body = "{\"routes\": [{\"type\": \"lobby\", \"address\": \"10.0.2.10:25565\"}]}"; // no "world"
        assertTrue(RoutesJson.parse(body).isEmpty());
    }
}
