package dev.foliasmp.routessync;

import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses {@code GET /api/v1/access-requests/approved-uuids}'s response
 * shape (PLAN.md §11C): {@code {"uuids": ["<uuid>", ...]}}.
 *
 * Same "not a general JSON parser" reasoning as {@link RoutesJson} — the
 * only quoted values in this response are UUIDs, so matching any quoted
 * hex-ish string of the right length is sufficient and avoids a JSON
 * library dependency.
 */
public final class ApprovedPlayers {
    private static final Pattern UUID_CANDIDATE = Pattern.compile("\"([0-9a-fA-F-]{32,36})\"");

    private ApprovedPlayers() {
    }

    public static Set<UUID> parse(String body) {
        Set<UUID> uuids = new HashSet<>();
        Matcher matcher = UUID_CANDIDATE.matcher(body);
        while (matcher.find()) {
            UUID uuid = tryParse(matcher.group(1));
            if (uuid != null) {
                uuids.add(uuid);
            }
        }
        return uuids;
    }

    /**
     * Mojang's API — and mgmt's stored copy of its response, PLAN.md §11C —
     * returns UUIDs without dashes; Velocity's {@code Player#getUniqueId()}
     * returns the dashed form. Normalize both to the same representation
     * before comparing, rather than requiring one specific format here.
     */
    private static UUID tryParse(String raw) {
        String cleaned = raw.replace("-", "");
        if (cleaned.length() != 32) {
            return null;
        }
        String dashed = cleaned.substring(0, 8) + "-" + cleaned.substring(8, 12) + "-"
                + cleaned.substring(12, 16) + "-" + cleaned.substring(16, 20) + "-" + cleaned.substring(20);
        try {
            return UUID.fromString(dashed);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }
}
