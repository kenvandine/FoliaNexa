package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;

/**
 * Polls {@code GET /api/v1/routes/display} on folia-nexa-mgmt. PLAN.md §7.
 */
public final class MgmtDisplayClient {
    private final MgmtHttpFetcher fetcher;

    public MgmtDisplayClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and keep the previously known
     *         display config rather than reverting to Velocity's own
     *         baked-in velocity.toml/server-icon.png on one transient
     *         mgmt outage.
     */
    public ProxyDisplay fetch() throws IOException, InterruptedException {
        return DisplayJson.parse(fetcher.get("/api/v1/routes/display"));
    }
}
