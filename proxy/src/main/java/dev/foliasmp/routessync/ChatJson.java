package dev.foliasmp.routessync;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.ArrayList;
import java.util.List;

/**
 * JSON for the chat bridge (PLAN.md §16): parses {@code GET
 * /api/v1/chat/pending}'s {@code [{"world": "..." | null, "author": "...",
 * "message": "...", "kind": "discord" | "broadcast"}]} and builds the body
 * {@code POST /api/v1/chat/report} expects. {@code kind} defaults to
 * {@code "discord"} when absent, matching mgmt's own Pydantic default —
 * lets an old mgmt build (predating operator broadcasts) keep working
 * against a newer proxy build without every message needing the field.
 * Real Gson, same reasoning as {@link DisplayJson} — chat messages are
 * free-form player-typed text, not the alphanumeric-only shape
 * {@link RoutesJson}'s regex approach assumes.
 */
public final class ChatJson {
    private ChatJson() {
    }

    public static List<PendingChatMessage> parsePending(String body) {
        List<PendingChatMessage> messages = new ArrayList<>();
        JsonArray array = JsonParser.parseString(body).getAsJsonArray();
        for (JsonElement element : array) {
            JsonObject obj = element.getAsJsonObject();
            String world = (obj.has("world") && !obj.get("world").isJsonNull()) ? obj.get("world").getAsString() : null;
            String kind = (obj.has("kind") && !obj.get("kind").isJsonNull()) ? obj.get("kind").getAsString() : "discord";
            messages.add(new PendingChatMessage(world, obj.get("author").getAsString(), obj.get("message").getAsString(), kind));
        }
        return messages;
    }

    public static String buildReportBody(String world, String player, String message) {
        JsonObject obj = new JsonObject();
        obj.addProperty("world", world);
        obj.addProperty("player", player);
        obj.addProperty("message", message);
        return obj.toString();
    }
}
