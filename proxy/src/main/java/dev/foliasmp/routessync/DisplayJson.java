package dev.foliasmp.routessync;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * Parses {@code GET /api/v1/routes/display}'s response shape:
 *
 * <pre>{"motd": "...", "icon_base64": "..." | null}</pre>
 *
 * Unlike {@link RoutesJson}, this uses real Gson rather than a regex
 * extractor: {@code motd} is free-form operator-entered MiniMessage text
 * (can contain quotes, backslashes, unicode) and {@code icon_base64} can
 * run to tens of kilobytes, neither of which the routes table's
 * no-escaping assumption holds for. Gson is only {@code compileOnly} here
 * (not a new dependency — it's already transitively on velocity-api's
 * compile classpath and genuinely bundled by Velocity at runtime, same as
 * the adventure/MiniMessage classes this plugin also uses without
 * declaring them directly).
 */
public final class DisplayJson {
    private DisplayJson() {
    }

    public static ProxyDisplay parse(String body) {
        JsonObject obj = JsonParser.parseString(body).getAsJsonObject();
        String motd = obj.get("motd").getAsString();
        String iconBase64 = (obj.has("icon_base64") && !obj.get("icon_base64").isJsonNull())
                ? obj.get("icon_base64").getAsString()
                : null;
        return new ProxyDisplay(motd, iconBase64);
    }
}
