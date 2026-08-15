package dev.foliasmp.routessync;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;

/**
 * Polls {@code GET /api/v1/routes} on folia-smp-mgmt. PLAN.md §7, §10.
 */
public final class MgmtRoutesClient {
    private final HttpClient http;
    private final URI routesUri;
    private final String apiToken;
    private final Duration timeout;

    public MgmtRoutesClient(String mgmtBaseUrl, String apiToken, Duration timeout) {
        this.http = HttpClient.newBuilder().connectTimeout(timeout).build();
        this.routesUri = URI.create(stripTrailingSlash(mgmtBaseUrl) + "/api/v1/routes");
        this.apiToken = apiToken;
        this.timeout = timeout;
    }

    private static String stripTrailingSlash(String url) {
        return url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }

    /**
     * @return the current routes, or {@code null} if the request failed —
     *         callers should keep the previous known-good state rather
     *         than tearing down every registered server on one transient
     *         mgmt outage.
     */
    public List<Route> fetch() throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(routesUri)
                .header("Authorization", "Bearer " + apiToken)
                .timeout(timeout)
                .GET()
                .build();

        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IOException("mgmt returned HTTP " + response.statusCode() + " for " + routesUri);
        }
        return RoutesJson.parse(response.body());
    }
}
