import React from "react";
import { Routes, Route } from "react-router-dom";
import Navbar from "./components/Navbar";
import Home from "./Pages/Home";
import Landing from "./Pages/Landing";
import DataExplorer from "./Pages/DataExplorer";

const App = () => {
  return (
    <>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/home" element={<Landing />} />
        <Route path="/Home" element={<Landing />} />
        <Route path="/chat" element={<Home />} />
        <Route path="/data" element={<DataExplorer />} />
      </Routes>
    </>
  );
};

export default App;
