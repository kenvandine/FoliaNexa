package dev.foliasmp.routessync;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Minimal, purpose-built parser for {@code GET /api/v1/routes}'s response
 * shape (PLAN.md §7):
 *
 * <pre>{"routes": [{"world": "...", "type": "...", "address": "host:port", "default": true|false}, ...]}</pre>
 *
 * This is <b>not</b> a general JSON parser. World/type/address values are
 * LXD instance names and host:port pairs — alphanumeric, hyphens, dots,
 * colons — which never need JSON string-escaping in practice, so a small
 * regex-based extractor is safer to get right than hand-rolling a full
 * parser, and avoids pulling in a JSON library at compile time (Velocity
 * bundles Gson at runtime, but pinning to a specific version here for one
 * narrow use isn't worth the coupling). If mgmt's API ever needs to emit
 * arbitrary/escaped text through this endpoint, replace this with Gson
 * instead of extending the regexes.
 */
public final class RoutesJson {
    private static final Pattern OBJECT = Pattern.compile("\\{[^{}]*}");
    private static final Pattern FIELD = Pattern.compile("\"(\\w+)\"\\s*:\\s*(?:\"([^\"]*)\"|(true|false))");
    // The top-level "domains" object (domain -> world name, PLAN.md §7C) is
    // a flat string:string map, same "never needs escaping" reasoning as
    // FIELD above — hostnames and world/LXD-instance names, not arbitrary
    // text — so it gets its own small pair of regexes rather than pulling
    // in a JSON library.
    private static final Pattern DOMAINS_OBJECT = Pattern.compile("\"domains\"\\s*:\\s*\\{([^{}]*)}");
    private static final Pattern DOMAIN_ENTRY = Pattern.compile("\"([^\"]+)\"\\s*:\\s*\"([^\"]+)\"");

    private RoutesJson() {
    }

    /**
     * Parses the top-level {@code "domains": {"host.example.com": "world-name", ...}}
     * map. Missing key or an empty object both parse to an empty map, not
     * an error — most clusters won't declare any host domains.
     */
    public static Map<String, String> parseDomainRoutes(String body) {
        Map<String, String> domainRoutes = new HashMap<>();
        Matcher objectMatcher = DOMAINS_OBJECT.matcher(body);
        if (!objectMatcher.find()) {
            return domainRoutes;
        }
        Matcher entryMatcher = DOMAIN_ENTRY.matcher(objectMatcher.group(1));
        while (entryMatcher.find()) {
            domainRoutes.put(entryMatcher.group(1), entryMatcher.group(2));
        }
        return domainRoutes;
    }

    public static List<Route> parse(String body) {
        List<Route> routes = new ArrayList<>();
        Matcher objectMatcher = OBJECT.matcher(body);
        while (objectMatcher.find()) {
            Route route = parseObject(objectMatcher.group());
            if (route != null) {
                routes.add(route);
            }
        }
        return routes;
    }

    private static Route parseObject(String obj) {
        Map<String, String> fields = new HashMap<>();
        Matcher fieldMatcher = FIELD.matcher(obj);
        while (fieldMatcher.find()) {
            String key = fieldMatcher.group(1);
            String stringValue = fieldMatcher.group(2);
            String literalValue = fieldMatcher.group(3);
            fields.put(key, stringValue != null ? stringValue : literalValue);
        }

        String world = fields.get("world");
        String address = fields.get("address");
        if (world == null || address == null) {
            return null; // not a route object
        }

        int colon = address.lastIndexOf(':');
        if (colon < 0) {
            return null;
        }
        String host = address.substring(0, colon);
        int port;
        try {
            port = Integer.parseInt(address.substring(colon + 1));
        } catch (NumberFormatException e) {
            return null;
        }

        return new Route(world, fields.getOrDefault("type", "unknown"), host, port, "true".equals(fields.get("default")));
    }
}
