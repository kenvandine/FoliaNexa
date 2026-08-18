package dev.foliasmp.routessync;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.List;
import java.util.Map;

/**
 * Builds the body {@code POST /api/v1/presence/report} expects (PLAN.md
 * §7A): {@code {"worlds": [{"world": "...", "players": [{"uuid": "...",
 * "username": "..."}]}]}}. Real Gson, same reasoning as {@link ChatJson} —
 * usernames are player-supplied text, not the alphanumeric-only shape
 * {@link RoutesJson}'s regex approach assumes.
 */
public final class PresenceJson {
    private PresenceJson() {
    }

    public static String buildReportBody(Map<String, List<OnlinePlayer>> worldPlayers) {
        JsonObject root = new JsonObject();
        JsonArray worlds = new JsonArray();
        for (Map.Entry<String, List<OnlinePlayer>> entry : worldPlayers.entrySet()) {
            JsonObject worldObj = new JsonObject();
            worldObj.addProperty("world", entry.getKey());
            JsonArray players = new JsonArray();
            for (OnlinePlayer player : entry.getValue()) {
                JsonObject playerObj = new JsonObject();
                playerObj.addProperty("uuid", player.uuid());
                playerObj.addProperty("username", player.username());
                players.add(playerObj);
            }
            worldObj.add("players", players);
            worlds.add(worldObj);
        }
        root.add("worlds", worlds);
        return root.toString();
    }
}
