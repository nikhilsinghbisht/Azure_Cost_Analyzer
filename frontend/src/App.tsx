import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom';
import { isAuthenticated } from './lib/auth';
import Dashboard from './pages/Dashboard';
import History from './pages/History';
import Login from './pages/Login';
import Report from './pages/Report';
import Signup from './pages/Signup';

function ProtectedRoute() {
  return isAuthenticated() ? <Outlet /> : <Navigate to="/login" replace />;
}

function PublicRoute() {
  return isAuthenticated() ? <Navigate to="/" replace /> : <Outlet />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public — redirect to dashboard if already logged in */}
        <Route element={<PublicRoute />}>
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />
        </Route>

        {/* Protected — redirect to login if not authenticated */}
        <Route element={<ProtectedRoute />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/report/:analysisId" element={<Report />} />
          <Route path="/history" element={<History />} />
        </Route>

        {/* Fallback */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
