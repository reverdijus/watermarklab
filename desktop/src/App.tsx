import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from "react";
import { HashRouter, Route, Routes, useLocation } from "react-router";
import { QueryClientProvider } from "@tanstack/react-query";
import { CircleAlert, Compass } from "lucide-react";
import { AppShell } from "./components/layout/AppShell";
import { LaunchProvider } from "./components/launch/LaunchProvider";
import { LoginGate } from "./components/LoginGate";
import { Button, Callout, ConfirmProvider, EmptyState, Page, PageSkeleton, ToastProvider } from "./components/ui";
import { queryClient } from "./lib/queries";
import { useThemeSync } from "./lib/theme";

const Home = lazy(() => import("./pages/Home"));
const RunsList = lazy(() => import("./pages/RunsList"));
const RunDetail = lazy(() => import("./pages/RunDetail"));
const ProjectPage = lazy(() => import("./pages/ProjectPage"));
const ExperimentEditor = lazy(() => import("./pages/ExperimentEditor"));
const HpoPlanEditor = lazy(() => import("./pages/HpoPlanEditor"));
const StudyDetail = lazy(() => import("./pages/StudyDetail"));
const Compare = lazy(() => import("./pages/Compare"));
const Leaderboard = lazy(() => import("./pages/Leaderboard"));
const Library = lazy(() => import("./pages/Library"));
const Datasets = lazy(() => import("./pages/Datasets"));
const Benchmarks = lazy(() => import("./pages/Benchmarks"));
const Hosts = lazy(() => import("./pages/Hosts"));
const Budget = lazy(() => import("./pages/Budget"));
const Settings = lazy(() => import("./pages/Settings"));

/** Catches rendering errors of a page so the shell stays usable. */
class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Erreur de rendu", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Page>
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Cette page a rencontré une erreur"
          action={
            <Button size="sm" onClick={() => this.setState({ error: null })}>
              Réessayer
            </Button>
          }
        >
          {this.state.error.message}
        </Callout>
      </Page>
    );
  }
}

function NotFound() {
  return (
    <Page>
      <EmptyState
        icon={Compass}
        title="Page introuvable"
        description="Ce lien ne correspond à aucune page de Watermark Lab."
        action={<Button to="/">Revenir à l'accueil</Button>}
      />
    </Page>
  );
}

function AppRoutes() {
  const location = useLocation();
  return (
    <PageErrorBoundary key={location.pathname}>
      <Suspense fallback={<PageSkeleton />}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/projects/:name" element={<ProjectPage />} />
          <Route path="/projects/:name/experiments/:exp" element={<ExperimentEditor />} />
          <Route path="/projects/:name/hpo/:plan" element={<HpoPlanEditor />} />
          <Route path="/runs" element={<RunsList />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/studies/:id" element={<StudyDetail />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
          <Route path="/library" element={<Library />} />
          <Route path="/datasets" element={<Datasets />} />
          <Route path="/benchmarks" element={<Benchmarks />} />
          <Route path="/hosts" element={<Hosts />} />
          <Route path="/budget" element={<Budget />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </PageErrorBoundary>
  );
}

export default function App() {
  useThemeSync();
  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <ToastProvider>
          <ConfirmProvider>
            <LoginGate>
              <LaunchProvider>
                <AppShell>
                  <AppRoutes />
                </AppShell>
              </LaunchProvider>
            </LoginGate>
          </ConfirmProvider>
        </ToastProvider>
      </HashRouter>
    </QueryClientProvider>
  );
}
