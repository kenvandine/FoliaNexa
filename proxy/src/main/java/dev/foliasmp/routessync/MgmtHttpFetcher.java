package dev.foliasmp.routessync;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Shared bearer-token HTTP client for talking to folia-nexa-mgmt — GET for
 * polling ({@link MgmtRoutesClient} §7, {@link AccessGateClient} §11C,
 * {@link MgmtDisplayClient}, chat/pending), POST for reporting
 * (chat/report, §16).
 */
final class MgmtHttpFetcher {
    // A pooled HTTP/1.1 keep-alive connection that goes silently
    // black-holed (socket looks alive, nothing ever answers again, no
    // clean close) can wedge the JDK's HttpClient indefinitely — confirmed
    // live 2026-08-17 against play.sullivan.linuxgroove.com: the WireGuard
    // tunnel itself never dropped in the outage window (no wg-quick
    // restart, no kernel link events, no VPS reboot), and mgmt answered
    // instantly the moment a *fresh* connection was tried, yet
    // folia-routes-sync kept timing out on every poll for over an hour
    // until the whole proxy process was restarted. Rebuilding the
    // HttpClient after a run of consecutive I/O failures gets the same
    // recovery without needing a manual restart. A non-200 response
    // doesn't count toward this — getting any HTTP response back at all
    // proves the connection itself is fine, so only send() actually
    // throwing (timeout, reset, etc.) is treated as a stuck-connection
    // signal.
    private static final int CONSECUTIVE_FAILURES_BEFORE_RESET = 3;

    private final String baseUrl;
    private final String apiToken;
    private final Duration timeout;
    private final AtomicInteger consecutiveFailures = new AtomicInteger();
    private volatile HttpClient http;

    MgmtHttpFetcher(String baseUrl, String apiToken, Duration timeout) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiToken = apiToken;
        this.timeout = timeout;
        this.http = newClient(timeout);
    }

    private static HttpClient newClient(Duration timeout) {
        // Force HTTP/1.1: the JDK client defaults to attempting an h2c
        // (HTTP/2 cleartext) upgrade first, which mgmt's uvicorn/h11
        // server doesn't support. GET requests limp through anyway (just
        // an "Unsupported upgrade request" warning server-side), but on
        // POST the request body comes through empty — mgmt sees `body=b''`
        // and 422s. Same bug class already hit and fixed for FoliaNexaStats
        // (catalog.yaml, "fixes the HTTP/2 upgrade bug").
        return HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(timeout).build();
    }

    String get(String path) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Authorization", "Bearer " + apiToken)
                .timeout(timeout)
                .GET()
                .build();
        return send(request, path).body();
    }

    void post(String path, String jsonBody) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Authorization", "Bearer " + apiToken)
                .header("Content-Type", "application/json")
                .timeout(timeout)
                .POST(HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8))
                .build();
        send(request, path);
    }

    private HttpResponse<String> send(HttpRequest request, String path) throws IOException, InterruptedException {
        HttpResponse<String> response;
        try {
            response = http.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            if (consecutiveFailures.incrementAndGet() >= CONSECUTIVE_FAILURES_BEFORE_RESET) {
                resetClient();
            }
            throw e;
        }
        consecutiveFailures.set(0);
        if (response.statusCode() != 200) {
            throw new IOException("mgmt returned HTTP " + response.statusCode() + " for " + path);
        }
        return response;
    }

    private synchronized void resetClient() {
        HttpClient old = http;
        http = newClient(timeout);
        consecutiveFailures.set(0);
        old.close();
    }

    // package-private, for MgmtHttpFetcherTest only
    HttpClient currentClient() {
        return http;
    }
}
