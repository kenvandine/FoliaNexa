package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Reports who's connected to which backend world (PLAN.md §7A) —
 * {@code POST /api/v1/presence/report}, from Velocity's own
 * {@code RegisteredServer.getPlayersConnected()}, the one accurate
 * source of per-world presence in this cluster.
 */
public final class PresenceClient {
    private final MgmtHttpFetcher fetcher;

    public PresenceClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and skip the poll cycle; the next
     *         successful report simply supersedes whatever mgmt has,
     *         there's no backlog to catch up on for a live snapshot.
     */
    public void report(Map<String, List<OnlinePlayer>> worldPlayers) throws IOException, InterruptedException {
        fetcher.post("/api/v1/presence/report", PresenceJson.buildReportBody(worldPlayers));
    }
}
