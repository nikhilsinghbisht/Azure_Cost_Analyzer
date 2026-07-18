import { Link, useNavigate, useLocation } from 'react-router-dom';
import { clearAuth, getUser } from '../lib/auth';

export default function Navbar() {
  const navigate = useNavigate();
  const location = useLocation();
  const user = getUser();

  function handleLogout() {
    clearAuth();
    navigate('/login');
  }

  const linkClass = (path: string) =>
    `text-sm font-medium transition-colors ${
      location.pathname === path
        ? 'text-white'
        : 'text-gray-400 hover:text-white'
    }`;

  return (
    <nav className="bg-gray-900 border-b border-gray-800 sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Logo */}
          <Link to="/" className="flex items-center gap-2.5 group">
            <div className="w-8 h-8 rounded-lg bg-azure flex items-center justify-center text-white font-bold text-sm group-hover:bg-azure-light transition-colors">
              AI
            </div>
            <span className="text-white font-semibold text-sm hidden sm:block">
              Cloud Cost Detective
            </span>
          </Link>

          {/* Nav links */}
          <div className="flex items-center gap-6">
            <Link to="/" className={linkClass('/')}>
              Dashboard
            </Link>
            <Link to="/history" className={linkClass('/history')}>
              History
            </Link>
          </div>

          {/* User + logout */}
          <div className="flex items-center gap-4">
            {user && (
              <span className="text-gray-400 text-xs hidden sm:block truncate max-w-[160px]">
                {user.email}
              </span>
            )}
            <button
              onClick={handleLogout}
              className="text-xs font-medium text-gray-400 hover:text-red-400 transition-colors border border-gray-700 hover:border-red-500/50 rounded-md px-3 py-1.5"
            >
              Logout
            </button>
          </div>
        </div>
      </div>
    </nav>
  );
}
