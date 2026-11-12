import React, { createContext, useContext, useState, useEffect, useCallback } from "react";

const ThemeContext = createContext();

export const ThemeProvider = ({ children }) => {
  const [theme, setThemeState] = useState(() => {
    // 1. Check local storage
    try {
      const stored = localStorage.getItem("intelliquery-theme");
      if (stored === "light" || stored === "dark") {
        return stored;
      }
    } catch (e) {
      // Ignore localStorage read errors
    }
    // 2. Default to system preference
    if (typeof window !== "undefined" && window.matchMedia) {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return "dark";
  });

  // Apply class and data-theme attribute to <html> element
  const applyTheme = useCallback((targetTheme) => {
    const root = document.documentElement;
    if (targetTheme === "dark") {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    root.setAttribute("data-theme", targetTheme);
  }, []);

  useEffect(() => {
    applyTheme(theme);
  }, [theme, applyTheme]);

  // Listen to OS system preference changes if user hasn't explicitly set localStorage
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");

    const handleChange = (e) => {
      try {
        const stored = localStorage.getItem("intelliquery-theme");
        if (!stored) {
          // No manual override, follow system
          const newSystemTheme = e.matches ? "dark" : "light";
          setThemeState(newSystemTheme);
          applyTheme(newSystemTheme);
        }
      } catch (err) {}
    };

    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [applyTheme]);

  // Set explicit theme and persist to localStorage
  const setTheme = (newTheme) => {
    setThemeState(newTheme);
    try {
      localStorage.setItem("intelliquery-theme", newTheme);
    } catch (e) {}
  };

  // Toggle between light and dark
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
  };

  return (
    <ThemeContext.Provider value={{ theme, isDark: theme === "dark", toggleTheme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
};

export const useTheme = () => {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
};

export default ThemeContext;
