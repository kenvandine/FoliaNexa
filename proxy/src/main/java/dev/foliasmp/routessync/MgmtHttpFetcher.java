package dev.foliasmp.routessync;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Shared bearer-token GET client for polling folia-nexa-mgmt — used by both
 * {@link MgmtRoutesClient} (§7) and {@link AccessGateClient} (§11C).
 */
final class MgmtHttpFetcher {
    private final HttpClient http;
    private final String baseUrl;
    private final String apiToken;
    private final Duration timeout;

    MgmtHttpFetcher(String baseUrl, String apiToken, Duration timeout) {
        this.http = HttpClient.newBuilder().connectTimeout(timeout).build();
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiToken = apiToken;
        this.timeout = timeout;
    }

    String get(String path) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Authorization", "Bearer " + apiToken)
                .timeout(timeout)
                .GET()
                .build();

        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IOException("mgmt returned HTTP " + response.statusCode() + " for " + path);
        }
        return response.body();
    }
}
