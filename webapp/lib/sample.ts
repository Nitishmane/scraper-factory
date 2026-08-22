import type { RankedTitle } from "./port";

// Rendered when Port credentials are unset, unreachable, or the blueprint is empty, so
// the app always demos. The banner on the page says when sample data is showing.
// Mirrors the live contract: top-10 shows + top-10 movies, `rank` 1..10 within each kind.
const SHOWS: [string, string, boolean][] = [
  ["Yellowstone", "Paramount Network", true],
  ["Breaking Bad", "AMC", false],
  ["The Walking Dead", "AMC", true],
  ["Chopped", "Food Network", false],
  ["The Office", "Comedy Central", false],
  ["90 Day Fiance", "TLC", true],
  ["Diners, Drive-Ins and Dives", "Food Network", false],
  ["Friends", "TV Land", false],
  ["Gold Rush", "Discovery", true],
  ["Property Brothers", "HGTV", false],
];

const MOVIES: [string, string, boolean][] = [
  ["Interstellar", "AMC", false],
  ["Forrest Gump", "Hallmark Channel", false],
  ["Jurassic Park", "AMC", false],
  ["Top Gun", "Paramount Network", false],
  ["Titanic", "OWN", false],
  ["The Godfather", "AMC", false],
  ["The Sixth Sense", "FX", false],
  ["Gladiator", "AMC", true],
  ["The Dark Knight", "TNT", false],
  ["Inception", "TNT", false],
];

function build(
  rows: [string, string, boolean][],
  kind: "show" | "movie"
): RankedTitle[] {
  return rows.map(([title, channel, is_new], i) => ({
    rank: i + 1,
    title,
    kind,
    score: Math.round((0.95 - i * 0.06) * 100) / 100,
    next_airing_utc: null,
    channel,
    provider: "philo",
    record_url: "https://streamingtvguides.com/Philo-TV/Guide",
    is_new,
    airings: 10 - i,
    published_at: null,
  }));
}

export const SAMPLE_TITLES: RankedTitle[] = [
  ...build(SHOWS, "show"),
  ...build(MOVIES, "movie"),
];
