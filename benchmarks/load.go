// A separate, dependency-free HTTP load generator. One request per connection at a time.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"sync"
	"time"
)

type sample struct {
	latencies []float64
	errors    int
	bytes     int64
}

func main() {
	url := flag.String("url", "", "Target URL")
	method := flag.String("method", "GET", "HTTP method")
	bodyFile := flag.String("body", "", "Request body file")
	duration := flag.Duration("duration", 3*time.Second, "Measurement duration")
	concurrency := flag.Int("concurrency", 32, "Concurrent connections")
	expected := flag.Int("status", 200, "Expected HTTP status")
	flag.Parse()
	var body []byte
	if *bodyFile != "" {
		var err error
		body, err = os.ReadFile(*bodyFile)
		if err != nil {
			panic(err)
		}
	}
	transport := &http.Transport{
		MaxIdleConns: *concurrency, MaxIdleConnsPerHost: *concurrency,
		MaxConnsPerHost: *concurrency, DisableCompression: true,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second}
	samples := make([]sample, *concurrency)
	var wg sync.WaitGroup
	start := time.Now()
	deadline := start.Add(*duration)
	for i := range samples {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			s := &samples[index]
			for time.Now().Before(deadline) {
				req, err := http.NewRequest(*method, *url, bytes.NewReader(body))
				if err != nil {
					panic(err)
				}
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("X-Token", "bench")
				req.Header.Set("Cookie", "session=bench")
				began := time.Now()
				response, err := client.Do(req)
				if err != nil {
					s.errors++
					continue
				}
				n, readErr := io.Copy(io.Discard, response.Body)
				response.Body.Close()
				if readErr != nil || response.StatusCode != *expected {
					s.errors++
				}
				s.bytes += n
				s.latencies = append(s.latencies, float64(time.Since(began).Nanoseconds())/1e6)
			}
		}(i)
	}
	wg.Wait()
	elapsed := time.Since(start).Seconds()
	var latencies []float64
	errors := 0
	var transferred int64
	for _, s := range samples {
		latencies = append(latencies, s.latencies...)
		errors += s.errors
		transferred += s.bytes
	}
	sort.Float64s(latencies)
	percentile := func(p float64) float64 {
		if len(latencies) == 0 {
			return 0
		}
		return latencies[int(float64(len(latencies)-1)*p)]
	}
	result := map[string]any{
		"requests": len(latencies), "errors": errors, "seconds": elapsed,
		"rps": float64(len(latencies)) / elapsed, "p50_ms": percentile(.5),
		"p95_ms": percentile(.95), "p99_ms": percentile(.99), "bytes": transferred,
	}
	encoded, _ := json.Marshal(result)
	fmt.Println(string(encoded))
	if errors != 0 {
		os.Exit(1)
	}
}
