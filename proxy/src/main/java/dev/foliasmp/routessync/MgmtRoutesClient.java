package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;
import java.util.List;

/**
 * Polls {@code GET /api/v1/routes} on folia-smp-mgmt. PLAN.md §7, §10.
 */
public final class MgmtRoutesClient {
    private final MgmtHttpFetcher fetcher;

    public MgmtRoutesClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and keep their previous
     *         known-good state rather than tearing down every registered
     *         server on one transient mgmt outage.
     */
    public List<Route> fetch() throws IOException, InterruptedException {
        return RoutesJson.parse(fetcher.get("/api/v1/routes"));
    }
}
