package main

import (
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
)

func main() {
	api := proxy("http://127.0.0.1:9000")
	console := proxy("http://127.0.0.1:9001")

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/console" {
			http.Redirect(w, r, "/console/", http.StatusTemporaryRedirect)
			return
		}
		if r.URL.Path == "/console/" || strings.HasPrefix(r.URL.Path, "/console/") {
			r.URL.Path = strings.TrimPrefix(r.URL.Path, "/console")
			if r.URL.Path == "" {
				r.URL.Path = "/"
			}
			console.ServeHTTP(w, r)
			return
		}
		api.ServeHTTP(w, r)
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "9000"
	}
	server := &http.Server{
		Addr:              ":" + port,
		Handler:           handler,
		ReadHeaderTimeout: 15 * time.Second,
	}
	log.Printf("MinIO API and Console proxy listening on :%s", port)
	log.Fatal(server.ListenAndServe())
}

func proxy(target string) *httputil.ReverseProxy {
	targetURL, err := url.Parse(target)
	if err != nil {
		log.Fatal(err)
	}
	return httputil.NewSingleHostReverseProxy(targetURL)
}
