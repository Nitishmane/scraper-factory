import { getTop20, type RankedTitle } from "@/lib/port";

// Render per request so the page always reflects the live Context Lake. (A cache:no-store
// fetch would otherwise be caught during build and the sample-data HTML frozen in place.)
export const dynamic = "force-dynamic";

// Times are stored UTC; the guide site displays America/New_York, so render ET with a label.
function fmtAiring(iso: string | null): string {
  if (!iso) return "airing this week";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return "airing this week";
  const day = t.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "America/New_York",
  });
  const time = t.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "America/New_York",
  });
  return `${day}, ${time} ET`;
}

function fmtStamp(iso: string | null): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return "";
  return t.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "America/New_York",
  });
}

// The ranker emits top-10 shows + top-10 movies with `rank` running 1..10 *within* each
// kind, so ranks repeat across kinds. Split by kind, then order each list by its own rank.
function groupByKind(titles: RankedTitle[]): {
  shows: RankedTitle[];
  movies: RankedTitle[];
} {
  const byRank = (a: RankedTitle, b: RankedTitle) => a.rank - b.rank;
  return {
    shows: titles.filter((t) => t.kind !== "movie").sort(byRank),
    movies: titles.filter((t) => t.kind === "movie").sort(byRank),
  };
}

function Card({ t }: { t: RankedTitle }) {
  const onPhilo = t.record_url.includes("philo.com");
  const hasLink = t.record_url && t.record_url !== "#";
  return (
    <a
      className="card"
      href={hasLink ? t.record_url : undefined}
      target={hasLink ? "_blank" : undefined}
      rel={hasLink ? "noopener noreferrer" : undefined}
    >
      <span className="rank">
        <span className="rank-num">#{t.rank}</span>
      </span>
      <span className="meta">
        <span className="title-row">
          <span className="title">{t.title}</span>
          {t.is_new && <span className="badge new">New</span>}
        </span>
        <span className="sub">
          {t.channel && <span>{t.channel}</span>}
          {t.channel && t.provider && <span className="sep">·</span>}
          {t.provider && <span>{t.provider}</span>}
          {(t.channel || t.provider) && <span className="sep">·</span>}
          <span className="airing">{fmtAiring(t.next_airing_utc)}</span>
          {t.airings > 1 && (
            <>
              <span className="sep">·</span>
              <span className="airings-count">{t.airings} airings</span>
            </>
          )}
        </span>
        <span className="card-footer">
          {t.score > 0 && <span className="score">score {t.score.toFixed(2)}</span>}
          {onPhilo ? (
            <span className="btn">Record on Philo →</span>
          ) : hasLink ? (
            <span className="btn ghost">View guide →</span>
          ) : (
            <span className="btn disabled">No link</span>
          )}
        </span>
      </span>
    </a>
  );
}

function Section({
  title,
  kind,
  items,
  emptyTitle,
  emptyBody,
}: {
  title: string;
  kind: "shows" | "movies";
  items: RankedTitle[];
  emptyTitle: string;
  emptyBody: string;
}) {
  return (
    <section className="section">
      <div className="section-head">
        <span className="section-title">
          <span className={`section-dot ${kind}`} />
          {title}
        </span>
        <span className="section-count">
          {items.length} {items.length === 1 ? "pick" : "picks"}
        </span>
      </div>
      {items.length > 0 ? (
        <div className="list">
          {items.map((t) => (
            <Card key={`${t.kind}-${t.rank}`} t={t} />
          ))}
        </div>
      ) : (
        <div className="empty">
          <strong>{emptyTitle}</strong>
          <p>{emptyBody}</p>
        </div>
      )}
    </section>
  );
}

export default async function Page() {
  const { titles, source } = await getTop20();
  const { shows, movies } = groupByKind(titles);
  const publishedAt = titles.find((t) => t.published_at)?.published_at ?? null;
  const stamp = fmtStamp(publishedAt);

  return (
    <main>
      <header className="hero">
        <span className="eyebrow">What to record this week</span>
        <h1>Top 10 shows &amp; movies</h1>
        <p className="stamp">
          Scraped guide data ranked by TMDB popularity across Philo, YouTube TV, and Sling
          {stamp ? ` · updated ${stamp} ET` : ""}
        </p>
        {source === "sample" && (
          <div className="banner">
            Showing sample data — run <code>make publish</code> in the factory to load the
            live ranking.
          </div>
        )}
      </header>

      <div className="columns">
        <Section
          title="Top 10 Shows"
          kind="shows"
          items={shows}
          emptyTitle="No shows in this week’s top 10"
          emptyBody="The ranker returned no series this run. Check back after the next scrape."
        />

        <Section
          title="Top 10 Movies"
          kind="movies"
          items={movies}
          emptyTitle="No movies in this week’s top 10"
          emptyBody="The ranker returned no movies this run. Check back after the next scrape."
        />
      </div>

      <footer>
        Built by the{" "}
        <a href="https://github.com/nitishmane/scraper-factory">Scraper Factory</a> — a
        self-healing scraping pipeline on Bright Data, Port, and SigNoz.
      </footer>
    </main>
  );
}
