#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

bool file_exists (const std::string &path) {
  std::ifstream file (path);
  return file.good ();
}

std::string read_text_file (const std::string &path) {
  std::ifstream file (path);
  if (!file) {
    throw std::runtime_error ("cache file open failed: " + path);
  }

  std::ostringstream buffer;
  buffer << file.rdbuf ();
  return buffer.str ();
}

pybind11::object parse_json_text (const std::string &text) {
  pybind11::object json_module = pybind11::module_::import ("json");
  return json_module.attr ("loads") (text);
}

pybind11::object load_cache_json (const std::string &cache_dir, const std::string &filename) {
  return parse_json_text (read_text_file (cache_dir + "/" + filename));
}

std::string required_keihan_file (const std::string &direction, const std::string &service_day) {
  if (direction == "to_home") {
    return "keihan_neyagawa_to_kadoma_" + service_day + ".json";
  }
  if (direction == "from_home") {
    return "keihan_kadoma_to_neyagawa_" + service_day + ".json";
  }
  throw std::runtime_error ("unknown direction: " + direction);
}

std::string required_monorail_file (const std::string &direction, const std::string &service_day) {
  if (direction == "to_home") {
    return "osaka_monorail_stop_times_" + service_day + ".json";
  }
  if (direction == "from_home") {
    return "osaka_monorail_to_kadoma_" + service_day + ".json";
  }
  throw std::runtime_error ("unknown direction: " + direction);
}

std::string path_for_cache_file (const std::string &cache_dir, const std::string &filename) {
  return cache_dir + "/" + filename;
}

void generate_missing_cache_file (const std::string &direction, const std::string &service_day, const std::string &mono_station, const std::string &filename) {
  pybind11::object generator = pybind11::module_::import ("generate_timetable_cache");
  pybind11::object service_date = generator.attr ("default_service_date") (service_day);

  if (filename == required_keihan_file ("to_home", service_day)) {
    generator.attr ("build_keihan_neyagawa_to_kadoma") (service_day, service_date);
    return;
  }
  if (filename == required_keihan_file ("from_home", service_day)) {
    generator.attr ("build_keihan_kadoma_to_neyagawa") (service_day, service_date);
    return;
  }
  if (filename == required_monorail_file ("to_home", service_day)) {
    generator.attr ("build_monorail_stop_times") (service_day, service_date);
    return;
  }
  if (filename == required_monorail_file ("from_home", service_day)) {
    generator.attr ("build_monorail_to_kadoma_station") (mono_station, service_day, service_date);
    return;
  }

  throw std::runtime_error ("unknown cache file: " + filename + " for " + direction);
}

void ensure_cache_file (const std::string &cache_dir, const std::string &direction, const std::string &service_day, const std::string &mono_station, const std::string &filename) {
  if (file_exists (path_for_cache_file (cache_dir, filename))) {
    return;
  }
  generate_missing_cache_file (direction, service_day, mono_station, filename);
}

pybind11::dict load_generated_cache_json (const std::string &cache_dir, const std::string &direction, const std::string &service_day, const std::string &mono_station, const std::string &filename) {
  ensure_cache_file (cache_dir, direction, service_day, mono_station, filename);
  return load_cache_json (cache_dir, filename).cast<pybind11::dict> ();
}

pybind11::dict load_required_caches_from (const pybind11::dict &request) {
  const std::string direction = request["direction"].cast<std::string> ();
  const std::string service_day = request["service_day"].cast<std::string> ();
  const std::string cache_dir = request["cache_dir"].cast<std::string> ();
  const std::string mono_station = request["mono_station"].cast<std::string> ();
  const std::string keihan_file = required_keihan_file (direction, service_day);
  const std::string monorail_file = required_monorail_file (direction, service_day);

  pybind11::dict caches;
  caches["keihan"] = load_generated_cache_json (cache_dir, direction, service_day, mono_station, keihan_file);
  caches["monorail"] = load_generated_cache_json (cache_dir, direction, service_day, mono_station, monorail_file);
  return caches;
}

int parse_time_to_minutes (const std::string &time_str) {
  return std::stoi (time_str.substr (0, 2)) * 60 + std::stoi (time_str.substr (3, 2));
}

std::string minutes_to_hhmm (int minutes) {
  int normalized = minutes % (24 * 60);
  if (normalized < 0) {
    normalized += 24 * 60;
  }
  std::ostringstream out;
  out << std::setfill ('0') << std::setw (2) << (normalized / 60) << ":" << std::setw (2) << (normalized % 60);
  return out.str ();
}

int departure_minutes_for_base (const std::string &time_text, int base_minutes) {
  int minutes = parse_time_to_minutes (time_text);
  if (minutes < 180 && base_minutes > 1200) {
    minutes += 24 * 60;
  }
  return minutes;
}

int arrival_after_departure (const std::string &time_text, int departure_minutes) {
  int minutes = parse_time_to_minutes (time_text);
  while (minutes < departure_minutes) {
    minutes += 24 * 60;
  }
  return minutes;
}

pybind11::dict make_segment (const std::string &mode, const std::string &line, const std::string &from, const std::string &to, const std::string &departure, const std::string &arrival, const std::string &type, const std::string &note) {
  pybind11::dict segment;
  segment["mode"] = mode;
  segment["line"] = line;
  segment["from"] = from;
  segment["to"] = to;
  segment["departure"] = departure;
  segment["arrival"] = arrival;
  segment["type"] = type;
  segment["note"] = note;
  return segment;
}

std::string keihan_arrival_text (const pybind11::dict &train, const std::string &target) {
  const std::string key = target == "門真市" ? "kadoma_arrival" : "neyagawa_arrival";
  if (train["route"].cast<std::string> () == "direct") {
    return train[key.c_str ()].cast<std::string> ();
  }
  pybind11::dict transfer = train["transfer_train"].cast<pybind11::dict> ();
  return transfer[key.c_str ()].cast<std::string> ();
}

void append_keihan_segments (pybind11::list &segments, const pybind11::dict &train, const std::string &origin, const std::string &target) {
  if (train["route"].cast<std::string> () == "direct") {
    segments.append (make_segment ("train", "京阪電車", origin, target, train["departure"].cast<std::string> (), keihan_arrival_text (train, target), train["type"].cast<std::string> (), "直通"));
    return;
  }

  pybind11::dict transfer = train["transfer_train"].cast<pybind11::dict> ();
  segments.append (make_segment ("train", "京阪電車", origin, "萱島", train["departure"].cast<std::string> (), train["kayashima_arrival"].cast<std::string> (), train["type"].cast<std::string> (), "萱島で乗換"));
  segments.append (make_segment ("train", "京阪電車", "萱島", target, transfer["kayashima_departure"].cast<std::string> (), keihan_arrival_text (train, target), transfer["type"].cast<std::string> (), ""));
}

pybind11::object find_first_train_after (const pybind11::list &trains, const std::string &key, int base_minutes) {
  for (pybind11::handle train_handle : trains) {
    pybind11::dict train = train_handle.cast<pybind11::dict> ();
    if (departure_minutes_for_base (train[key.c_str ()].cast<std::string> (), base_minutes) >= base_minutes) {
      return pybind11::reinterpret_borrow<pybind11::object> (train_handle);
    }
  }
  return pybind11::none ();
}

pybind11::dict route_response (const std::string &origin, const std::string &destination, int start_minutes, int end_minutes, const pybind11::list &segments, const std::string &kind) {
  pybind11::dict response;
  response["ok"] = true;
  response["engine"] = "cpp";
  response["kind"] = kind;
  response["origin"] = origin;
  response["destination"] = destination;
  response["start_time"] = minutes_to_hhmm (start_minutes);
  response["end_time"] = minutes_to_hhmm (end_minutes);
  response["total_minutes"] = end_minutes - start_minutes;
  response["segments"] = segments;
  return response;
}

pybind11::dict error_response (const std::string &message) {
  pybind11::dict response;
  response["ok"] = false;
  response["message"] = message;
  return response;
}

struct Candidate {
  bool ok = false;
  int start_minutes = 0;
  int end_minutes = 0;
  pybind11::object first_train = pybind11::none ();
  pybind11::object second_train = pybind11::none ();
};

int candidate_duration (const Candidate &candidate) {
  return candidate.end_minutes - candidate.start_minutes;
}

bool better_shortest (const Candidate &candidate, const Candidate &best) {
  if (!best.ok) {
    return true;
  }
  const int duration = candidate_duration (candidate);
  const int best_duration = candidate_duration (best);
  if (duration != best_duration) {
    return duration < best_duration;
  }
  if (candidate.end_minutes != best.end_minutes) {
    return candidate.end_minutes < best.end_minutes;
  }
  return candidate.start_minutes < best.start_minutes;
}

void collect_shortest_candidate (const Candidate &candidate, std::vector<Candidate> &routes, Candidate &best) {
  if (!candidate.ok) {
    return;
  }
  if (!best.ok || candidate_duration (candidate) < candidate_duration (best)) {
    best = candidate;
    routes.clear ();
    routes.push_back (candidate);
    return;
  }
  if (candidate_duration (candidate) == candidate_duration (best)) {
    routes.push_back (candidate);
    if (better_shortest (candidate, best)) {
      best = candidate;
    }
  }
}

Candidate make_to_home_candidate (const pybind11::dict &request, const pybind11::dict &caches, const pybind11::object &keihan_obj, int start_minutes) {
  const std::string station = request["mono_station"].cast<std::string> ();
  const int school_walk_min = request["school_walk_min"].cast<int> ();
  const int transfer_min = request["transfer_min"].cast<int> ();
  const int home_walk_min = request["home_walk_min"].cast<int> ();

  pybind11::dict keihan_train = keihan_obj.cast<pybind11::dict> ();
  const int neyagawa_ready = start_minutes + school_walk_min;
  const int keihan_departure = departure_minutes_for_base (keihan_train["departure"].cast<std::string> (), neyagawa_ready);
  if (keihan_departure < neyagawa_ready) {
    return {};
  }

  const int kadoma_ready = arrival_after_departure (keihan_arrival_text (keihan_train, "門真市"), keihan_departure) + transfer_min;

  pybind11::dict mono_data = caches["monorail"].cast<pybind11::dict> ();
  pybind11::list mono_trains = mono_data["trains"].cast<pybind11::list> ();
  pybind11::object mono_obj = find_first_train_after (mono_trains, "departure", kadoma_ready);
  if (mono_obj.is_none ()) {
    return {};
  }

  pybind11::dict mono_train = mono_obj.cast<pybind11::dict> ();
  pybind11::dict stops = mono_train["stops"].cast<pybind11::dict> ();
  if (!stops.contains (station.c_str ()) || stops[station.c_str ()].is_none ()) {
    return {};
  }

  const int mono_departure = departure_minutes_for_base (mono_train["departure"].cast<std::string> (), kadoma_ready);
  const int mono_arrival = arrival_after_departure (stops[station.c_str ()].cast<std::string> (), mono_departure);

  Candidate candidate;
  candidate.ok = true;
  candidate.start_minutes = start_minutes;
  candidate.end_minutes = mono_arrival + home_walk_min;
  candidate.first_train = keihan_obj;
  candidate.second_train = mono_obj;
  return candidate;
}

Candidate make_from_home_candidate (const pybind11::dict &request, const pybind11::dict &caches, const pybind11::object &mono_obj, int start_minutes) {
  const int home_walk_min = request["home_walk_min"].cast<int> ();
  const int transfer_min = request["transfer_min"].cast<int> ();
  const int school_walk_min = request["school_walk_min"].cast<int> ();

  pybind11::dict mono_train = mono_obj.cast<pybind11::dict> ();
  const int station_ready = start_minutes + home_walk_min;
  const int mono_departure = departure_minutes_for_base (mono_train["departure"].cast<std::string> (), station_ready);
  if (mono_departure < station_ready) {
    return {};
  }

  const int kadoma_ready = arrival_after_departure (mono_train["kadoma_arrival"].cast<std::string> (), mono_departure) + transfer_min;

  pybind11::dict keihan_data = caches["keihan"].cast<pybind11::dict> ();
  pybind11::list keihan_trains = keihan_data["trains"].cast<pybind11::list> ();
  pybind11::object keihan_obj = find_first_train_after (keihan_trains, "departure", kadoma_ready);
  if (keihan_obj.is_none ()) {
    return {};
  }

  pybind11::dict keihan_train = keihan_obj.cast<pybind11::dict> ();
  const int keihan_departure = departure_minutes_for_base (keihan_train["departure"].cast<std::string> (), kadoma_ready);
  const int neyagawa_arrival = arrival_after_departure (keihan_arrival_text (keihan_train, "寝屋川市"), keihan_departure);

  Candidate candidate;
  candidate.ok = true;
  candidate.start_minutes = start_minutes;
  candidate.end_minutes = neyagawa_arrival + school_walk_min;
  candidate.first_train = mono_obj;
  candidate.second_train = keihan_obj;
  return candidate;
}

pybind11::dict build_to_home_response (const pybind11::dict &request, const Candidate &candidate, const std::string &kind) {
  const std::string station = request["mono_station"].cast<std::string> ();
  const int school_walk_min = request["school_walk_min"].cast<int> ();
  const int home_walk_min = request["home_walk_min"].cast<int> ();

  pybind11::dict keihan_train = candidate.first_train.cast<pybind11::dict> ();
  pybind11::dict mono_train = candidate.second_train.cast<pybind11::dict> ();
  pybind11::dict stops = mono_train["stops"].cast<pybind11::dict> ();
  const std::string mono_arrival = stops[station.c_str ()].cast<std::string> ();

  pybind11::list segments;
  segments.append (make_segment ("walk", "徒歩", "学校", "寝屋川市", minutes_to_hhmm (candidate.start_minutes), minutes_to_hhmm (candidate.start_minutes + school_walk_min), "", std::to_string (school_walk_min) + "分"));
  append_keihan_segments (segments, keihan_train, "寝屋川市", "門真市");
  segments.append (make_segment ("train", "大阪モノレール", "門真市", station, mono_train["departure"].cast<std::string> (), mono_arrival, mono_train["type"].cast<std::string> (), ""));
  segments.append (make_segment ("walk", "徒歩", station, "自宅", mono_arrival, minutes_to_hhmm (candidate.end_minutes), "", std::to_string (home_walk_min) + "分"));

  return route_response ("学校", "自宅", candidate.start_minutes, candidate.end_minutes, segments, kind);
}

pybind11::dict build_from_home_response (const pybind11::dict &request, const Candidate &candidate, const std::string &kind) {
  const std::string station = request["mono_station"].cast<std::string> ();
  const int home_walk_min = request["home_walk_min"].cast<int> ();
  const int school_walk_min = request["school_walk_min"].cast<int> ();

  pybind11::dict mono_train = candidate.first_train.cast<pybind11::dict> ();
  pybind11::dict keihan_train = candidate.second_train.cast<pybind11::dict> ();
  const std::string neyagawa_arrival = keihan_arrival_text (keihan_train, "寝屋川市");

  pybind11::list segments;
  segments.append (make_segment ("walk", "徒歩", "自宅", station, minutes_to_hhmm (candidate.start_minutes), minutes_to_hhmm (candidate.start_minutes + home_walk_min), "", std::to_string (home_walk_min) + "分"));
  segments.append (make_segment ("train", "大阪モノレール", station, "門真市", mono_train["departure"].cast<std::string> (), mono_train["kadoma_arrival"].cast<std::string> (), mono_train["type"].cast<std::string> (), ""));
  append_keihan_segments (segments, keihan_train, "門真市", "寝屋川市");
  segments.append (make_segment ("walk", "徒歩", "寝屋川市", "学校", neyagawa_arrival, minutes_to_hhmm (candidate.end_minutes), "", std::to_string (school_walk_min) + "分"));

  return route_response ("自宅", "学校", candidate.start_minutes, candidate.end_minutes, segments, kind);
}

pybind11::dict build_to_home_shortest_response (const pybind11::dict &request, const std::vector<Candidate> &routes, const Candidate &best, const std::string &kind) {
  pybind11::dict response = build_to_home_response (request, best, kind);
  pybind11::list route_list;
  for (const Candidate &route : routes) {
    route_list.append (build_to_home_response (request, route, kind));
  }
  response["routes"] = route_list;
  response["route_count"] = static_cast<int> (routes.size ());
  return response;
}

pybind11::dict build_from_home_shortest_response (const pybind11::dict &request, const std::vector<Candidate> &routes, const Candidate &best, const std::string &kind) {
  pybind11::dict response = build_from_home_response (request, best, kind);
  pybind11::list route_list;
  for (const Candidate &route : routes) {
    route_list.append (build_from_home_response (request, route, kind));
  }
  response["routes"] = route_list;
  response["route_count"] = static_cast<int> (routes.size ());
  return response;
}

pybind11::dict find_to_home (const pybind11::dict &request, const pybind11::dict &caches) {
  const std::string kind = request["kind"].cast<std::string> ();
  const int now_minutes = request["now_minutes"].cast<int> ();
  const int school_walk_min = request["school_walk_min"].cast<int> ();

  pybind11::dict keihan_data = caches["keihan"].cast<pybind11::dict> ();
  pybind11::list keihan_trains = keihan_data["trains"].cast<pybind11::list> ();
  Candidate best;
  std::vector<Candidate> shortest_routes;

  if (kind == "fastest_arrival") {
    pybind11::object keihan_obj = find_first_train_after (keihan_trains, "departure", now_minutes + school_walk_min);
    if (!keihan_obj.is_none ()) {
      best = make_to_home_candidate (request, caches, keihan_obj, now_minutes);
    }
  } else if (kind == "shortest_arrival") {
    for (pybind11::handle train_handle : keihan_trains) {
      pybind11::dict keihan_train = train_handle.cast<pybind11::dict> ();
      const int keihan_departure = departure_minutes_for_base (keihan_train["departure"].cast<std::string> (), now_minutes);
      const int start_minutes = keihan_departure - school_walk_min;
      Candidate candidate = make_to_home_candidate (request, caches, pybind11::reinterpret_borrow<pybind11::object> (train_handle), start_minutes);
      collect_shortest_candidate (candidate, shortest_routes, best);
    }
  } else {
    throw std::runtime_error ("unknown kind: " + kind);
  }

  if (!best.ok) {
    return error_response ("利用できる経路が見つかりませんでした");
  }
  if (kind == "shortest_arrival") {
    return build_to_home_shortest_response (request, shortest_routes, best, kind);
  }
  return build_to_home_response (request, best, kind);
}

pybind11::dict find_from_home (const pybind11::dict &request, const pybind11::dict &caches) {
  const std::string kind = request["kind"].cast<std::string> ();
  const std::string station = request["mono_station"].cast<std::string> ();
  const int now_minutes = request["now_minutes"].cast<int> ();
  const int home_walk_min = request["home_walk_min"].cast<int> ();

  pybind11::dict mono_data = caches["monorail"].cast<pybind11::dict> ();
  pybind11::dict by_station = mono_data["by_station"].cast<pybind11::dict> ();
  if (!by_station.contains (station.c_str ())) {
    const std::string service_day = request["service_day"].cast<std::string> ();
    const std::string cache_dir = request["cache_dir"].cast<std::string> ();
    const std::string monorail_file = required_monorail_file ("from_home", service_day);
    generate_missing_cache_file ("from_home", service_day, station, monorail_file);
    mono_data = load_cache_json (cache_dir, monorail_file).cast<pybind11::dict> ();
    by_station = mono_data["by_station"].cast<pybind11::dict> ();
    if (!by_station.contains (station.c_str ())) {
      return error_response (station + " -> 門真市 のモノレールキャッシュがありません");
    }
  }

  pybind11::dict station_data = by_station[station.c_str ()].cast<pybind11::dict> ();
  pybind11::list mono_trains = station_data["trains"].cast<pybind11::list> ();
  Candidate best;
  std::vector<Candidate> shortest_routes;

  if (kind == "fastest_arrival") {
    pybind11::object mono_obj = find_first_train_after (mono_trains, "departure", now_minutes + home_walk_min);
    if (!mono_obj.is_none ()) {
      best = make_from_home_candidate (request, caches, mono_obj, now_minutes);
    }
  } else if (kind == "shortest_arrival") {
    for (pybind11::handle train_handle : mono_trains) {
      pybind11::dict mono_train = train_handle.cast<pybind11::dict> ();
      const int mono_departure = departure_minutes_for_base (mono_train["departure"].cast<std::string> (), now_minutes);
      const int start_minutes = mono_departure - home_walk_min;
      Candidate candidate = make_from_home_candidate (request, caches, pybind11::reinterpret_borrow<pybind11::object> (train_handle), start_minutes);
      collect_shortest_candidate (candidate, shortest_routes, best);
    }
  } else {
    throw std::runtime_error ("unknown kind: " + kind);
  }

  if (!best.ok) {
    return error_response ("利用できる経路が見つかりませんでした");
  }
  if (kind == "shortest_arrival") {
    return build_from_home_shortest_response (request, shortest_routes, best, kind);
  }
  return build_from_home_response (request, best, kind);
}

pybind11::dict find_route (const pybind11::dict &request) {
  pybind11::dict caches = load_required_caches_from (request);
  const std::string direction = request["direction"].cast<std::string> ();

  if (direction == "to_home") {
    return find_to_home (request, caches);
  }
  if (direction == "from_home") {
    return find_from_home (request, caches);
  }
  throw std::runtime_error ("unknown direction: " + direction);
}

}  // namespace

PYBIND11_MODULE (route_core, m) {
  m.doc () = "Route search core.";
  m.def ("find_route", &find_route, pybind11::arg ("request"));
}
