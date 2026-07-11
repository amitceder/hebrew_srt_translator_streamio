Manual source subtitles (highest priority)
==========================================

Drop a known-good English (or Arabic) .srt here and the addon will translate
THAT exact file to Hebrew, instead of guessing via OpenSubtitles.

This is the reliable path when OpenSubtitles has no synced match for your
exact release (e.g. a YTS WEBRip with no moviehash entry), or when you already
downloaded a perfectly-synced English sub from the OpenSubtitles v3 addon.

Naming (any of these work) - match by IMDb id OR by the played filename:

  Movies:
    tt0427340.srt
    tt0427340.en.srt

  Series episodes:
    tt1234567.S01E02.srt

  By exact played filename (drop the video extension):
    Masters.Of.The.Universe.2026.RETAIL.DKSUBS.1080p.WEBRip.x264.AAC-[YTS.GG - YTS.BZ].srt

Notes:
- The file must be a real .srt (SubRip). Not .zip, not .ass.
- After adding/replacing a file, on the TV: fully close the movie and reopen
  it so Stremio re-requests the subtitle.
- Manual files are checked BEFORE any cache or OpenSubtitles lookup, so they
  always win.
