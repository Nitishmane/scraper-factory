import { SAMPLE_TITLES } from "./sample";

export type RankedTitle = {
  rank: number;
  title: string;
  kind: "movie" | "show";
  score: number;
  next_airing_utc: string | null;
  channel: string | null;
  provider: string | null;
  record_url: string;
  is_new: boolean;
  airings: number;
  published_at: string | null;
};

const API = process.env.PORT_API_BASE ?? "https://api.getport.io/v1";

// Module-scope token cache; Port access tokens live ~1h, ISR hits this once a minute.
let token: string | null = null;
let tokenExpiry = 0;

async function getToken(): Promise<string> {
  if (token && Date.now() < tokenExpiry - 60_000) return token;
  const resp = await fetch(`${API}/auth/access_token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      clientId: process.env.PORT_CLIENT_ID,
      clientSecret: process.env.PORT_CLIENT_SECRET,
    }),
    cache: "no-store",
  });
  if (!resp.ok) throw new Error(`Port auth failed: ${resp.status}`);
  const body = (await resp.json()) as { accessToken: string; expiresIn?: number };
  token = body.accessToken;
  tokenExpiry = Date.now() + (body.expiresIn ?? 3600) * 1000;
  return token;
}

type PortEntity = {
  identifier: string;
  updatedAt?: string;
  properties: Record<string, unknown>;
};

export async function getTop20(): Promise<{
  titles: RankedTitle[];
  source: "db" | "sample";
}> {
  if (!process.env.PORT_CLIENT_ID || !process.env.PORT_CLIENT_SECRET) {
    return { titles: SAMPLE_TITLES, source: "sample" };
  }
  try {
    const resp = await fetch(`${API}/blueprints/ranked_title/entities`, {
      headers: { Authorization: `Bearer ${await getToken()}` },
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`Port entities fetch failed: ${resp.status}`);
    const body = (await resp.json()) as { entities?: PortEntity[] };
    const titles = (body.entities ?? [])
      .map((e): RankedTitle => {
        const p = e.properties;
        return {
          rank: Number(p.rank ?? 0),
          title: String(p.show_title ?? e.identifier),
          kind: p.kind === "movie" ? "movie" : "show",
          score: Number(p.score ?? 0),
          next_airing_utc: (p.next_airing_at as string) ?? null,
          channel: (p.channel as string) ?? null,
          provider: (p.provider as string) ?? null,
          record_url: String(p.record_url ?? "#"),
          is_new: Boolean(p.is_new),
          airings: Number(p.airings ?? 0),
          published_at: e.updatedAt ?? null,
        };
      })
      .filter((t) => t.rank > 0);
    // The ranker now emits top-10 shows + top-10 movies, so `rank` is 1..10 *within*
    // each kind and repeats across kinds. Grouping (not global rank) defines the two
    // lists; the page sorts each group by rank. Cap generously in case the lake grows.
    const capped = titles.slice(0, 40);
    if (capped.length === 0) return { titles: SAMPLE_TITLES, source: "sample" };
    return { titles: capped, source: "db" };
  } catch (err) {
    // A cache:no-store fetch during a static build throws DYNAMIC_SERVER_USAGE; swallowing
    // it lets Next freeze the sample-data HTML. Rethrow so the route is marked dynamic.
    if (
      err &&
      typeof err === "object" &&
      "digest" in err &&
      (err as { digest?: unknown }).digest === "DYNAMIC_SERVER_USAGE"
    ) {
      throw err;
    }
    console.error("Context Lake read failed, serving sample data:", err);
    return { titles: SAMPLE_TITLES, source: "sample" };
  }
}
