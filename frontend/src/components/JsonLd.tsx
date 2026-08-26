/**
 * Renders a JSON-LD structured-data block inside a `<script>` tag.
 *
 * Server component — emitted in the raw HTML during SSR so crawlers can read
 * it without executing JS. Pass a plain schema.org object (see
 * @/lib/structured-data).
 */
export default function JsonLd({ data }: { data: Record<string, unknown> | Array<Record<string, unknown>> }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }}
    />
  );
}
