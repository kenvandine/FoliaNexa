package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;
import java.util.Set;
import java.util.UUID;

/**
 * Polls {@code GET /api/v1/access-requests/approved-uuids} on
 * folia-nexa-mgmt. PLAN.md §11C.
 */
public final class AccessGateClient {
    private final MgmtHttpFetcher fetcher;

    public AccessGateClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and keep the previously known
     *         approved set rather than locking everyone out on one
     *         transient mgmt outage.
     */
    public Set<UUID> fetchApprovedUuids() throws IOException, InterruptedException {
        return ApprovedPlayers.parse(fetcher.get("/api/v1/access-requests/approved-uuids"));
    }
}
