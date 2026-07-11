# Stremio Hebrew AI subtitle addon

The existing Flask service now also exposes a Stremio subtitle addon at:

```text
https://YOUR_PUBLIC_HOST/manifest.json
```

The addon receives the video hash, file size, filename, and IMDb episode ID
from Stremio. It finds an English or Arabic source subtitle through
OpenSubtitles, then runs the existing Google baseline + Groq contextual polish
pipeline. The translated SRT is cached on the server, so repeated playback
does not translate the same file again.

## Required server settings

Set these on the HTPC before starting the container:

```bash
export GROQ_API_KEY='your-groq-key'
export OPEN_SUBTITLES_API_KEY='your-opensubtitles-api-key'
export OPEN_SUBTITLES_USER_AGENT='HebrewAIStremioAddon v0.1'
export STREMIO_PUBLIC_BASE_URL='https://your-public-host'
export STREMIO_TOKEN_SECRET='a-long-random-secret'
```

`STREMIO_PUBLIC_BASE_URL` must be the HTTPS URL that Stremio on the phone and
TV can reach. A private `10.x.x.x` address is suitable for local diagnostics,
but it cannot be used for a community-listed addon.

## Start the existing container

```bash
./run.sh
```

Verify the service before installing it:

```bash
curl -fsS https://your-public-host/healthz
curl -fsS https://your-public-host/manifest.json
```

Install by pasting the manifest URL into Stremio's addon search field. The
addon appears as `Hebrew AI Subtitles` and offers `Hebrew AI (Google + Groq)`
when Stremio asks for subtitles for a matching movie or episode.

## Recommended personal setup

For your own phone and TV, keep the addon private and install its stable HTTPS
manifest URL directly in Stremio. This avoids the extra Community catalog
publication step. The HTPC still needs to stay online, and the HTTPS hostname
must remain unchanged so the addon stays attached to your Stremio account.

The Community catalog is useful only if you want other Stremio users to find
the addon. It does not host the service or remove the HTPC/tunnel dependency.

## Community listing

After the public HTTPS endpoint is working, publish the manifest URL to
Stremio's central addon announcement endpoint:

```bash
curl -fsS \
  -H 'Content-Type: application/json' \
  -d '{"transportUrl":"https://your-public-host/manifest.json","transportName":"http"}' \
  -X POST https://api.strem.io/api/addonPublish
```

The endpoint should be tested first. A public listing does not make the
translator work if the HTPC or the OpenSubtitles/Groq credentials are offline.
