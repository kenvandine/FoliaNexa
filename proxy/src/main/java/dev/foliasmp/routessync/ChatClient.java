package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;
import java.util.List;

/**
 * Both directions of the Discord chat bridge (PLAN.md §16) — reports
 * outgoing (game->Discord) chat and polls incoming (Discord->game)
 * messages queued by folia-nexa-mgmt.
 */
public final class ChatClient {
    private final MgmtHttpFetcher fetcher;

    public ChatClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should treat one failed report as an acceptable
     *         dropped chat message, not something worth retrying (this
     *         runs once per chat message, on a poll-adjacent cadence
     *         retrying would just build up a backlog under a real outage).
     */
    public void report(String world, String player, String message) throws IOException, InterruptedException {
        fetcher.post("/api/v1/chat/report", ChatJson.buildReportBody(world, player, message));
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and just skip this poll cycle;
     *         mgmt's own queue (routers/chat.py) keeps accumulating
     *         messages until the next successful poll drains it.
     */
    public List<PendingChatMessage> fetchPending() throws IOException, InterruptedException {
        return ChatJson.parsePending(fetcher.get("/api/v1/chat/pending"));
    }
}
