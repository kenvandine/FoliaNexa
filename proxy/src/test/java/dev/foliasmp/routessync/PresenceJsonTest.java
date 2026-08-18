package dev.foliasmp.routessync;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static com.google.gson.JsonParser.parseString;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PresenceJsonTest {

    @Test
    void buildsReportBodyWithWorldsAndPlayers() {
        Map<String, List<OnlinePlayer>> worldPlayers = Map.of(
                "world-overworld", List.of(new OnlinePlayer("uuid-a", "Alice"), new OnlinePlayer("uuid-b", "Bob"))
        );
        String body = PresenceJson.buildReportBody(worldPlayers);

        JsonObject root = parseString(body).getAsJsonObject();
        var worlds = root.getAsJsonArray("worlds");
        assertEquals(1, worlds.size());
        JsonObject world = worlds.get(0).getAsJsonObject();
        assertEquals("world-overworld", world.get("world").getAsString());
        var players = world.getAsJsonArray("players");
        assertEquals(2, players.size());
        assertEquals("uuid-a", players.get(0).getAsJsonObject().get("uuid").getAsString());
        assertEquals("Alice", players.get(0).getAsJsonObject().get("username").getAsString());
    }

    @Test
    void emptyWorldStillReportsWithEmptyPlayerList() {
        Map<String, List<OnlinePlayer>> worldPlayers = Map.of("world-lobby", List.of());
        String body = PresenceJson.buildReportBody(worldPlayers);

        JsonObject world = parseString(body).getAsJsonObject().getAsJsonArray("worlds").get(0).getAsJsonObject();
        assertEquals("world-lobby", world.get("world").getAsString());
        assertTrue(world.getAsJsonArray("players").isEmpty());
    }

    @Test
    void noWorldsProducesEmptyWorldsArray() {
        String body = PresenceJson.buildReportBody(Map.of());
        assertTrue(parseString(body).getAsJsonObject().getAsJsonArray("worlds").isEmpty());
    }

    @Test
    void usernameContainingQuotesRoundTrips() {
        Map<String, List<OnlinePlayer>> worldPlayers = Map.of(
                "world-overworld", List.of(new OnlinePlayer("uuid-a", "a\"b"))
        );
        String body = PresenceJson.buildReportBody(worldPlayers);
        JsonObject world = parseString(body).getAsJsonObject().getAsJsonArray("worlds").get(0).getAsJsonObject();
        String username = world.getAsJsonArray("players").get(0).getAsJsonObject().get("username").getAsString();
        assertEquals("a\"b", username);
    }
}
