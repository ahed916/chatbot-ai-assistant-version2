import { Navigate } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";

interface Props {
  children: React.ReactNode;
  requiredRole: "admin" | "project_manager";
}

export function ProtectedRoute({ children, requiredRole }: Props) {
  const { session, role, loading } = useAuth();

  if (loading) return <div>Loading...</div>;
  if (!session) return <Navigate to="/login" replace />;
  if (role !== requiredRole) return <Navigate to="/login" replace />;

  return <>{children}</>;
}