package dev.foliasmp.routessync;

import java.io.IOException;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Polls {@code GET /api/v1/routes} on folia-nexa-mgmt. PLAN.md §7, §10.
 */
public final class MgmtRoutesClient {
    private final MgmtHttpFetcher fetcher;

    public MgmtRoutesClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.fetcher = new MgmtHttpFetcher(mgmtBaseUrl, apiToken, timeout);
    }

    /**
     * Both the per-world route list and the domain->world map (PLAN.md
     * §7C) come from the same response body, fetched once per poll — two
     * separate HTTP calls could observe two different mgmt states.
     */
    public record RoutesFetch(List<Route> routes, Map<String, String> domainRoutes) {
    }

    /**
     * @throws IOException on a non-200 response or a network failure —
     *         callers should catch this and keep their previous
     *         known-good state rather than tearing down every registered
     *         server on one transient mgmt outage.
     */
    public RoutesFetch fetch() throws IOException, InterruptedException {
        String body = fetcher.get("/api/v1/routes");
        return new RoutesFetch(RoutesJson.parse(body), RoutesJson.parseDomainRoutes(body));
    }
}
