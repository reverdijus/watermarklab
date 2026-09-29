import { Gauge } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Benchmarks() {
  return (
    <Page>
      <PageHeader title="Benchmarks" />
      <EmptyState icon={Gauge} title="Bientôt disponible" description="Suites d'évaluation figées et versionnées." />
    </Page>
  );
}
